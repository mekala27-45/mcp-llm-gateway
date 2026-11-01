# mcp-llm-gateway

[![ci](https://github.com/mekala27-45/mcp-llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mekala27-45/mcp-llm-gateway/actions/workflows/ci.yml)

Four parts of an agent security stack, built against mocks so all of it runs offline. A customer MCP server on stdio with strict schemas (Task 1), a gateway that authenticates MCP calls and keeps admin tools from non admins (Task 2), and an OpenAI compatible LLM gateway that redacts PII from replies as they stream (Task 3), limits each tenant by tokens per minute and fails over to a backup provider (Task 4).

```
packages/customer_server   Task 1: the MCP server, on the official Python SDK over stdio
packages/mock_downstream   the MCP server behind the gateway, with an admin tool and a call log
packages/mcp_gateway       Task 2: the MCP security gateway
packages/llm_gateway       Tasks 3 and 4: the redactor, the token limiter and the router
packages/mock_providers    OpenAI compatible streaming providers with set delays, 429s and PII
packages/gateway_common    JSON logs on stderr, the SSE parser, a background server for tests
demo/run.py, bench/        make demo and make bench
```

## How to run

Python 3.12 and uv. Mocks stand in for every server and provider, so nothing needs a key or a network once the packages are installed.

```
make install     uv sync --frozen
make demo        every task against the mocks, one line per check
make test        the whole suite, with coverage
make check       ruff, mypy strict and the tests, as CI runs them
make bench       the streaming numbers below, written to results/bench.json
```

One task at a time, its part of the demo and then its tests:

```
make task1       the customer server's self test over stdio
make task2       a viewer refused and an admin allowed through the gateway
make task3       a card and an email split across chunks, arriving redacted
make task4       a failover after a slow primary, a 429 and an estimate over the limit
```

`make demo` prints this (the timings move a little between runs):

```
task 1  refund of 25.00 for CUST-10001 accepted as RF-000001
task 1  refund of 25.001 accepted without rounding; remaining balance 369.999
task 1  refund of 0 refused with -32602 Invalid params: amount greater_than
task 2  viewer calls admin_reset_key: -32001 Unauthorized Tool Call, calls on the server 0
task 2  admin calls admin_reset_key: allowed, calls on the server 1
task 3  the provider sent the card in 4 chunks: ' 4111' ' 1111' ' 1111' ' 1111'
task 3  and the email in 7, one ending in 'e.co' with 'm' to come
task 3  the client got: Your refund is on its way. We sent the receipt to [REDACTED] and charged the card [REDACTED]. The SSN on file ends the same as [REDACTED].
task 4  primary silent for 3.5 s, dropped at 3 s, backup done at 3.1 s: primary:timeout,backup:ok
task 4  while it held 40,000 tokens, a request for 20,000 got 429, retry after 60 s
task 4  max_tokens 60,000 is more than a minute allows: 413 estimate_over_limit
logs: data/demo/gateways.log, request log: data/demo/llm_gateway.db
```

To use the customer server from a desktop MCP client, add it to the client's list of stdio servers. Most clients keep that list under mcpServers in a JSON config file:

```json
{
  "mcpServers": {
    "customers": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-llm-gateway", "customer-server"]
    }
  }
}
```

Adding `"env": {"VALIDATION_ERRORS": "tool"}` to that entry gives the SEP-1303 behavior described below.

The gateways can be run by hand as well:

```
export GATEWAY_JWT_SECRET=$(openssl rand -hex 32)
uv run python -m mock_downstream --port 8101 &
uv run mcp-gateway --port 8100 &
uv run mcp-gateway-token --role viewer     # Bearer token for a client at http://127.0.0.1:8100/mcp

export LLM_GATEWAY_SECRET=$(openssl rand -hex 32)
uv run python -m mock_providers --port 8201 --name primary &
uv run python -m mock_providers --port 8202 --name backup &
BACKUP_BASE_URL=http://127.0.0.1:8202/v1 uv run llm-gateway serve --port 8200 &
KEY=$(uv run llm-gateway add-tenant acme)  # the tenant's API key, printed once
curl -N http://127.0.0.1:8200/v1/chat/completions -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{"model": "mock", "stream": true, "messages": [{"role": "user", "content": "hi"}]}'
```

The LLM gateway also reads a local .env, which git ignores, so a real OpenAI compatible provider can be tried with PRIMARY_BASE_URL, PRIMARY_API_KEY and PRIMARY_MODEL. Nothing in the tests or the demo uses one.

## Where each criterion is answered

Code is under packages/NAME/src/NAME/ and tests under packages/NAME/tests/.

| Task | Criterion | Code | Tests |
|---|---|---|---|
| 1 | stdout carries only JSON-RPC | customer_server/guard.py, \_\_main\_\_.py | test_stdout.py, test_guard.py |
| 1 | error mapping and execution flow | customer_server/server.py | test_tools.py, test_stdio.py |
| 1 | schemas and malformed input | customer_server/models.py, samples.py | test_models.py, test_stdio.py |
| 2 | parsing the JSON-RPC wire format | mcp_gateway/inspection.py | test_inspection.py |
| 2 | proxy middleware and forwarding | mcp_gateway/app.py, downstream.py | test_gateway.py |
| 2 | method level authorization, clean errors | mcp_gateway/auth.py, inspection.py | test_bypass.py, test_auth.py, test_policies.py |
| 3 | stream chunking and buffer state | llm_gateway/redact.py, stream.py | test_redact_properties.py, test_rewrite.py |
| 3 | matching over partial text | llm_gateway/redact.py | test_redact.py, test_redos.py |
| 3 | memory and latency | llm_gateway/redact.py, bench/stream_bench.py | test_redact.py, make bench |
| 4 | concurrency and timeout races | llm_gateway/router.py, app.py | test_races.py, test_window.py |
| 4 | window eviction and token tracking | llm_gateway/limiter.py, db.py, estimate.py | test_limiter.py, test_tenancy.py |
| 4 | fallback and sanitized errors | llm_gateway/router.py, errors.py, app.py | test_failover.py |

test_stdio.py drives the server as a subprocess with the SDK client and runs every malformed case in samples.py. test_stdout.py reads the raw bytes on stdout while a test only tool prints, writes to descriptor 1 and starts a child that prints.

## Decisions

Task 1

- I read CUST-XXXXX as five digits, `^CUST-[0-9]{5}$`. If letters are meant too, it is one constant, CUSTOMER_ID_PATTERN in customer_server/models.py.
- A schema failure is JSON-RPC -32602 Invalid params by default, with data listing each failing field and rule and never the rejected value, because the task asks for standard JSON-RPC errors. SEP-1303 moved the specification the other way: a validation failure is a tool execution error with isError true, so the model can read it and retry with fixed arguments. VALIDATION_ERRORS=tool switches to that, and both modes are tested. I would keep the default when the caller is code or a gateway that acts on error codes, and use tool mode when a model calls the server directly and self correction is worth more than a code.
- I used the SDK's low level Server, not MCPServer. MCPServer reports argument failures as isError results before my code runs, so -32602 would have meant working around it. With the low level server, validation is one function that raises MCPError with INVALID_PARAMS, and the schemas in tools/list are still generated from the Pydantic models.
- The server claims its descriptors in \_\_main\_\_.py before the SDK is imported: the transport gets private copies of 0 and 1, descriptor 1 then points at stderr, descriptor 0 at /dev/null, and sys.stdout is sys.stderr. The SDK's stdio_server can claim them too, but only once it has been imported, and the import is where a stray print or a C extension writes. Passing the private descriptors to stdio_server as its streams also skips its own claim.
- The refund schema follows the assessment: any finite positive JSON number and a reason of at least 10 characters. It does not impose a $5,000 cap, a two-decimal restriction, a reason length ceiling or whitespace normalization. Amounts are converted from the parsed float's shortest decimal representation, stored without cent rounding, and returned as decimal strings. The mock's available-balance check is a business error, not a schema error. Exact decimal arithmetic also preserves very small accepted refunds in the remaining balance.
- A repeated identical refund within 60 seconds returns the original refund id instead of refunding twice, because agents retry.

Task 2

- Unauthorized Tool Call is -32001 because the task asks for it, and it fits the 2026-07-28 error code policy, which leaves -32000 to -32019 to implementations and keeps -32020 and up for the specification (HeaderMismatch is -32020). The Python SDK's client also uses -32001, for a request of its own that timed out, so a client should read the message and the data (the tool and the role) as well as the code. It goes out with HTTP 403.
- Every decision comes from the parsed body. The body is parsed once: duplicate keys are refused at any depth, because the downstream parser may keep the other one; NaN and Infinity are refused; an array is refused, since batching left the protocol in 2025; anything over 64 KiB is a 413. The gateway then forwards the object it checked, serialized again, not the client's bytes. Mcp-Method and Mcp-Name are only compared with the body, and a mismatch is -32020 with nothing forwarded.
- Before the admin_ check the tool name is NFKC normalized, case folded and stripped of format characters (Unicode category Cf: zero width spaces and joiners, the byte order mark, bidi controls). NFKC does not turn a Cyrillic a into a Latin one, so a name must also pass the SEP-986 character set, and a look-alike fails there.
- The client's Authorization header never goes downstream. The gateway sends its own credential, GATEWAY_DOWNSTREAM_TOKEN, or none, and the mock downstream refuses anything else, so a passthrough would fail the tests.
- Two policies, off by default because each changes what clients see. GATEWAY_HIDE_ADMIN_TOOLS removes admin_ tools from a viewer's tools/list; the call is refused either way, so this only stops advertising them. GATEWAY_LISTED_TOOLS_ONLY forwards only names the downstream listed in its tools/list, kept for 30 seconds; it fails closed when the listing cannot be read, which trades availability for a tighter allow list.

Task 3

- After each delta the redactor finds the earliest position where a match could still start or still grow, sends everything before it with complete matches replaced, and holds the rest. A complete match that touches the end of the buffer is held too, because more characters can extend it: an email ending in .co may become .com, and 13 digits that pass Luhn may be the first 13 of 16. It goes out when the next character proves it cannot grow, or when the stream ends.
- The pending position comes from a partial search with the regex module, but not the plain one: a plain partial search prefers any complete match, even a shorter one, over a longer path that is still growing. So I search with the same patterns plus a tail that can never match, and every path still alive at the end of the text shows up as partial.
- Every quantifier is bounded, so no match can be longer than 345 characters: a 64 character local part, the @, four 63 character labels with their dots and a 24 letter top level domain. RFC 5321 stops an address at 254, but the hold has to cover what the pattern can match, or a long match would be cut in two. A test feeds the longest candidate one character at a time and checks the hold never passes the bound, and the bench reports the largest hold seen.
- The price is latency on ordinary words. Any run of letters could be the local part of an email, so it waits for the next character that cannot be in one. The bench measures it.
- Card candidates are 13 to 19 digits with single spaces or dashes between them, kept only if they pass Luhn. When a run fails, I try its prefixes that end at a separator, shortest first, so a card followed by more digits is still found and an SSN after a failed run is not swallowed.
- Each choice index and each tool call's arguments get their own redactor, and every other field of a chunk is passed on as it came. Held text that a finish releases goes out with the finishing chunk, or in a small chunk of its own just before it.

Task 4

- The limiter is a sliding window log, one row per request in window_entries, not a counter. A fixed window counter is cheaper but admits up to twice the limit across a window boundary. The log evicts exactly what is 60 seconds old, and Retry-After comes from when enough tokens leave the window. Rows go at a tenant's next admission, so the table holds about a minute of requests per active tenant.
- Admission is one BEGIN IMMEDIATE transaction: evict, sum, decide and reserve with the write lock held, so two processes cannot both read the same window and both find room. SQLite runs in WAL mode with a busy timeout and one connection per process. Opening retries the switch to WAL, because SQLite refuses it at once, without waiting, while another process is writing the same new file.
- I reserve, then reconcile. Admission reserves the prompt estimate plus max_tokens for each of the `n` requested completions (one by default); `n` must be a positive integer; a request without max_tokens gets 1,024, which is sent on to the provider, so every reservation has a bound. After the reply the reservation becomes the usage the provider reported, or the prompt estimate plus the counted output when it reports none, and it is released if the call failed before any tokens were used. Usage above the reservation is recorded as it is, so the tenant pays for it in the next window instead of the limiter under counting. A request whose estimate alone is over 50,000 gets 413 estimate_over_limit, not a 429 it could never get past.
- The gateway asks for usage on every stream (stream_options.include_usage) and drops that extra chunk when the client did not ask for it.
- Prompt tokens are estimated by UTF-8 length by default. A byte level BPE tokenizer never makes more tokens than bytes, so this can only overestimate, and it needs nothing downloaded. LLM_GATEWAY_TIKTOKEN=1 switches to tiktoken's o200k_base, which downloads its vocabulary on first use with no timeout, so it is off in a repository meant to run offline. Either way it is an estimate for models that are not OpenAI's, and reconciling with real usage corrects it.
- The 3,000 ms timeout covers connect, headers and the first byte of the body for a stream, and the whole reply otherwise. Headers alone say nothing about tokens coming. Once the first byte is in, the stream belongs to that provider: part of the answer may already be with the client, and a second provider would start a different answer, so a stream that breaks ends with one error event and no failover.
- A 429, a timeout, a refused connection, a 5xx or any other failure goes to the backup, except 400, 413 and 422: those say the request itself is wrong, and the backup would say the same. A 429 with Retry-After moves that provider to the back of the order for that long, and it is still tried if nothing else answers. A reply that arrives after its provider was given up on is closed with its connection and never read.
- Every error a client sees has one shape, {"error": {"type", "code", "message", "request_id"}}, built by one exception handler. The endpoint turns any exception that is not a GatewayError into a generic internal error before it reaches the handler, so no upstream body, exception text or stack can reach a client. The traceback goes to the stderr log.

## Numbers

From make bench on 1 November 2026, 01:33 UTC, on an Intel Xeon Processor @ 2.80GHz with 2 cores and 7.8 GB, Linux, Python 3.12.3. The raw output is results/bench.json. For the latency rows the mock streams 300 characters as 79 tokens, one every 20 ms, and each row is 60 requests.

| | p50 | p99 |
|---|---|---|
| time to first token, straight to the mock | 22.08 ms | 24.7 ms |
| time to first token, through the gateway | 66.92 ms | 74.86 ms |
| delay the holdback adds to each character | 26.9 ms | 57.6 ms |

The reply starts with Thanks, which comes in two tokens and could still be the start of an email until the space at the front of the third token arrives. That one word is most of the difference in time to first token; after that a character waits for the end of its word.

| Redactor on its own, MB per second | chunks of 4 | chunks of 16 | chunks of 64 | largest hold |
|---|---|---|---|---|
| prose with no PII | 3.1 | 11.14 | 35.58 | 8 characters |
| an email, a card and an SSN every 150 characters | 0.77 | 1.05 | 0.86 | 25 characters |

The gateway process used 38.7 MB before a 54.7 MB stream with no PII and 39.3 MB after it; the stream took 4 s. Nothing in the path keeps the reply, so memory does not grow with its length.

## Security notes

| Threat | Control | Tests |
|---|---|---|
| spoofed or expired tokens | HS256 with a key from the environment; signature, exp, iss, aud and the role claim checked; alg none refused; one Bearer header only | test_auth.py, test_bypass.py |
| tampered payloads | strict parse; NFKC, case folding, Cf removal and the SEP-986 name check before the admin_ test; name must be a string | test_bypass.py, test_inspection.py |
| duplicate keys, header mismatch | duplicate keys refused at any depth; Mcp-Method and Mcp-Name must match the body (-32020) | test_inspection.py, test_bypass.py |
| token passthrough | client Authorization never forwarded; each gateway uses its own credential upstream | test_bypass.py, test_tenancy.py |
| error leakage | one error shape per gateway; nothing copied from an upstream body or exception; tracebacks only on stderr | test_failover.py, test_gateway.py, test_stdout.py |
| PII in streams | the holdback redactor and its chunking invariant | test_redact_properties.py, test_redaction.py |
| PII and secrets in logs | logs carry counts, token fingerprints and ids; tenant keys stored only as HMACs; the request log has no content | test_redaction.py, test_gateway.py, test_tenants.py, test_tenancy.py |
| oversized bodies | read in pieces and refused past 64 KiB (MCP) and 1 MiB (LLM) with 413 | test_bypass.py, test_proxy.py |
| ReDoS | every quantifier bounded, the email's possessive and anchored at the start of a run, a fast path for text with no @ or digit | test_redos.py |
| the limiter under load | BEGIN IMMEDIATE admission on SQLite in WAL, shared by processes | test_window.py, test_races.py |
| admin tools | role from the signed token only; -32001 before any downstream call; optional hiding from listings | test_gateway.py, test_bypass.py, test_policies.py |
| stdout corruption | descriptor level guard before any import | test_stdout.py, test_guard.py |

## What I would do next

- The data is mock data: an in memory customer table and refund ledger, a mock MCP server and mock providers. A real ledger would sit in a database, with the 60 second idempotency key as a unique constraint.
- HS256 with a shared secret is the default because it runs anywhere offline. In production I would verify RS256 or ES256 tokens against the identity provider's JWKS, with key rotation, and keep HS256 for tests.
- The limiter is single node: SQLite takes one writer at a time, which is fine for one host and a few processes. Across hosts I would move the window to Redis, a sorted set per tenant with a Lua script that evicts, sums and reserves in one step, or to Postgres, the same transaction with a row lock per tenant. Both keep reserve then reconcile; Redis gives up some durability for speed.
- PII detection is regular expressions. It misses names, postal addresses and phone numbers; SSNs written as nine digits in a row, which I left out because too many other numbers look like that; card numbers broken up by anything other than single spaces or dashes; emails with quoted or non ASCII local parts or a local part over 64 characters; and anything spelled out or paraphrased. The holdback works for any detector that can say where a match could still start, so the next step would be adding an entity recognizer for names and addresses behind it.
- The limit is one number for every tenant. Real tenants want their own limits, separate input and output budgets, and weights per model.
- The router has two providers in a fixed order. I would add a circuit breaker with half open probes, and order by recent latency and errors.

# Adding a real vendor adapter

The MCP tools never talk to a vendor. They call `FieldServiceAdapter`
(`src/field_service_mcp/adapters/base.py`). Supporting a real field service
product means writing one class that implements that interface. Nothing in
`server.py`, `schedule.py` or the tool schemas changes.

Two complete adapters ship with the repo and are worth reading first:

| File | What it shows |
| --- | --- |
| `src/field_service_mcp/adapters/mock_rest.py` | An HTTP adapter: auth header, retries, paging translation, error translation, mapping functions |
| `tests/in_memory_adapter.py` | The smallest possible adapter, about 100 lines, no HTTP |

## What an adapter is responsible for

1. **Calling the vendor.** Auth, base URL, timeouts, rate limits.
2. **Translating vocabulary.** The domain models in `models.py` say customer, job,
   visit and quote. Your vendor may say client, work order, appointment and
   estimate. Money here is a float in major units. Datetimes are naive local times.
   Convert in both directions inside the adapter and nowhere else.
3. **Translating failures.** Raise the classes in `errors.py`. The MCP layer
   turns them into tool errors the calling model can act on.

| Raise | When | What the model is told |
| --- | --- | --- |
| `NotFoundError` | The id does not exist | Check the id with a list tool |
| `InvalidRequestError` | The vendor rejected the arguments | Fix the arguments and retry |
| `ConflictError` | A business rule refused the write, such as a double booking | Look at the schedule and pick different values |
| `AuthError` | Credentials are wrong or expired | A person must fix the configuration |
| `UpstreamError` | Timeouts, 5xx, rate limits | Retrying later may work |

Anything else that escapes a tool is treated by the SDK as a crash, and the
model sees only a generic message. That is deliberate: unexpected exception
text can carry vendor internals.

## The contract in detail

- `list_*` take `limit` and `offset` and return `Page(items, total, limit, offset)`.
  If your vendor pages by number or by cursor, convert. `MockRestAdapter._list`
  shows offset to page-number conversion, including the case where the window
  straddles two vendor pages. For cursor APIs, walk forward and discard, and
  keep `limit` small.
- `list_visits(start, end)` uses overlap semantics on a half open window: a
  visit matches when `visit.start < end and visit.end > start`. Results are
  ordered earliest first.
- `create_visit` must raise `ConflictError` when the technician is already
  booked in an overlapping window. If the vendor does not enforce that, check
  with `list_visits` inside the adapter before posting. Be aware that a check
  followed by a write is not atomic.
- `create_job` starts in status `open`. `create_quote` starts in `draft`.
- Retry reads, never writes. A repeated POST can create a duplicate job.
  If the vendor supports idempotency keys, send one and then retrying writes
  becomes safe.
- `aclose()` releases the HTTP client.

You do not implement schedule views. `get_schedule` and `find_open_slots` are
computed in `schedule.py` from `list_visits`, `list_technicians`, `get_job`
and `get_customer`, so every adapter gets them.

## Steps

### 1. Write the class

```python
# src/field_service_mcp/adapters/example_vendor.py   (a stand-in name for your vendor)
import httpx

from ..config import Settings
from ..errors import AuthError, NotFoundError, UpstreamError
from ..models import Customer, Page
from .base import FieldServiceAdapter


def customer_from_vendor(row: dict) -> Customer:
    return Customer(id=str(row["uuid"]), name=row["full_name"], phone=row.get("mobile"), email=row.get("email"))


class ExampleVendorAdapter(FieldServiceAdapter):
    name = "example-vendor"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExampleVendorAdapter":
        return cls(settings.base_url, settings.api_key, settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_customer(self, customer_id: str) -> Customer:
        response = await self._client.get(f"/customers/{customer_id}")
        if response.status_code == 404:
            raise NotFoundError(f"Customer {customer_id} does not exist.")
        if response.status_code in (401, 403):
            raise AuthError("The vendor rejected the API key.")
        if response.status_code >= 500:
            raise UpstreamError("The vendor is unavailable.")
        return customer_from_vendor(response.json())

    # ... the other abstract methods. Python refuses to instantiate the class
    # until every one of them exists, so nothing can be forgotten silently.
```

Keep the mapping functions (`customer_from_vendor` and friends) as plain module
level functions. They are where most bugs live and they are trivial to unit
test with a captured payload.

### 2. Select it

Either register a short name in `adapters/__init__.py`:

```python
register_adapter("example-vendor", ExampleVendorAdapter.from_settings)
```

or leave the package untouched and point at the class:

```bash
export FSM_ADAPTER=my_company.adapters.example_vendor:ExampleVendorAdapter
export FSM_BASE_URL=https://api.vendor.example
export FSM_API_KEY=...   # from your secret store, never from the repo
```

### 3. Run the contract tests against it

`tests/test_adapter_contract.py` is parametrized over a dictionary of adapter
factories. Add yours, pointed at the vendor's sandbox:

```python
ADAPTERS = {
    "mock": lambda: MockRestAdapter(backoff_seconds=0),
    "in-memory": InMemoryAdapter,
    "example-vendor": lambda: ExampleVendorAdapter(os.environ["VENDOR_SANDBOX_URL"], os.environ["VENDOR_SANDBOX_KEY"], 10),
}
```

The contract tests create records, so only ever aim them at a sandbox. Guard
the entry with `pytest.mark.skipif` when the sandbox variables are missing so
the default test run stays offline.

### 4. Start read-only

Run with `FSM_READ_ONLY=true` first. The create tools are not registered at
all in that mode, so an agent cannot write no matter what it is asked. Turn
writes on after the read paths have been checked against real records.

## Things real vendors do that the mock does not

- **OAuth with token refresh.** Put the refresh inside the adapter's request
  helper, and raise `AuthError` only when the refresh itself fails.
- **Rate limits.** Honour `Retry-After` on 429 before retrying reads.
- **GraphQL.** The interface does not care. Build the query in the adapter.
- **Time zones.** If the vendor returns UTC, convert to the business's local
  time in the mapping functions and strip the offset.
- **Missing features.** If a vendor has no quotes, raise `InvalidRequestError`
  from the quote methods with a message saying so, rather than returning
  empty pages that look like real answers.

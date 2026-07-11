"""A Splitwise MCP server for Convoke — one API key, many groups.

Records shared expenses in the bot account's Splitwise groups so the agent can
track who owes whom. It calls the Splitwise REST API directly with a personal
API key — no third-party server ever holds your token — and targets any group
the bot belongs to **by name**, so one shim serves every Telegram group.

There is no server-side default group: which group a chat uses is the agent's
job to remember (in Convoke's chat memory) and pass on each call.

Key design point: the bot's own account is **never a participant** in the
expenses it creates. Splitwise's API (unlike its app UI) accepts an expense
whose user list omits the authenticated caller, so the humans carry the real
shares and the bot stays out of the split. The bot account only needs to be a
*member* of each group (for permission) and to hold the API key.

Tools: list_groups, list_members, record_expense, who_owes_whom, record_payment.

Environment:
  SPLITWISE_API_KEY  personal API key from https://secure.splitwise.com/apps
  PORT               HTTP port to serve on (default 8000)
"""

import os
from decimal import Decimal, ROUND_DOWN

import httpx
from fastmcp import FastMCP

API = "https://secure.splitwise.com/api/v3.0"

_client = httpx.Client(
    base_url=API,
    headers={"Authorization": f"Bearer {os.environ['SPLITWISE_API_KEY']}"},
    timeout=20.0,
)

mcp = FastMCP("Splitwise")


def _member_name(m: dict) -> str:
    return " ".join(x for x in (m.get("first_name"), m.get("last_name")) if x).strip()


def _groups() -> list[dict]:
    r = _client.get("/get_groups")
    r.raise_for_status()
    return r.json()["groups"]


def _resolve_group(group: str | int) -> int:
    """Turn a group name or id into a group id."""
    if isinstance(group, int) or str(group).isdigit():
        return int(group)
    groups = _groups()
    for g in groups:
        if g["name"].lower() == str(group).lower():
            return g["id"]
    known = ", ".join(g["name"] for g in groups) or "(none)"
    raise ValueError(f"No group named {group!r}. The bot's groups are: {known}.")


def _group(group_id: int) -> dict:
    """Fetch a group fresh so newly-joined members resolve without a restart."""
    r = _client.get(f"/get_group/{group_id}")
    r.raise_for_status()
    return r.json()["group"]


def _resolve_member(name: str, members: list[dict]) -> int:
    """Match a spoken name to a member id (case-insensitive, first name or full)."""
    n = name.strip().lower()
    for m in members:
        full = _member_name(m).lower()
        if n == full or n == full.split(" ")[0]:
            return m["id"]
    known = ", ".join(_member_name(m) for m in members)
    raise ValueError(f"No group member matches {name!r}. Members are: {known}")


def _even_split(cost: Decimal, n: int) -> list[Decimal]:
    """Split `cost` n ways to the penny; drop any remainder cents on the first shares."""
    base = (cost / n).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = [base] * n
    for i in range(int(((cost - base * n) * 100).to_integral_value())):
        shares[i] += Decimal("0.01")
    return shares


def _post_expense(data: dict) -> dict:
    """POST create_expense and surface real failures — Splitwise returns HTTP 200
    with a non-empty `errors` object even when it rejects the expense."""
    r = _client.post("/create_expense", data=data)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise ValueError(f"Splitwise rejected the expense: {body['errors']}")
    return body["expenses"][0]


@mcp.tool
def list_groups() -> list[dict]:
    """List the Splitwise groups the bot belongs to, as {id, name}. Pass a name as
    the `group` argument to the other tools to target one."""
    return [{"id": g["id"], "name": g["name"]} for g in _groups()]


@mcp.tool
def list_members(group: str) -> list[dict]:
    """List the people in `group` (name or id) as {id, name}."""
    return [{"id": m["id"], "name": _member_name(m)}
            for m in _group(_resolve_group(group))["members"]]


@mcp.tool
def record_expense(
    description: str,
    cost: str,
    paid_by: str,
    split_among: list[str],
    group: str,
) -> dict:
    """Log a shared expense in `group`: `paid_by` paid `cost` in total, split
    equally among `split_among` (member names). The bot is never part of the split.

    Example — record_expense("Dinner", "90.00", "Alice", ["Alice","Bob","Carol"])
    → Alice paid 90.00; Alice, Bob and Carol each owe 30.00.
    """
    group_id = _resolve_group(group)
    members = _group(group_id)["members"]
    total = Decimal(cost).quantize(Decimal("0.01"))
    payer_id = _resolve_member(paid_by, members)
    owe_ids = [_resolve_member(name, members) for name in split_among]

    owed = dict(zip(owe_ids, _even_split(total, len(owe_ids))))
    paid = {payer_id: total}

    data: dict = {"cost": str(total), "description": description, "group_id": group_id}
    for i, uid in enumerate(dict.fromkeys([payer_id, *owe_ids])):  # de-duped, ordered
        data[f"users__{i}__user_id"] = uid
        data[f"users__{i}__paid_share"] = str(paid.get(uid, Decimal("0.00")))
        data[f"users__{i}__owed_share"] = str(owed.get(uid, Decimal("0.00")))

    created = _post_expense(data)
    return {"id": created["id"], "cost": str(total), "description": description}


@mcp.tool
def who_owes_whom(group: str) -> list[str]:
    """`group`'s (name or id) current simplified balances, as plain sentences."""
    group_data = _group(_resolve_group(group))
    names = {m["id"]: _member_name(m) for m in group_data["members"]}
    lines = [
        f"{names.get(d['from'], d['from'])} owes "
        f"{names.get(d['to'], d['to'])} {d['amount']} {d['currency_code']}"
        for d in group_data.get("simplified_debts", [])
    ]
    return lines or ["Everyone is settled up."]


@mcp.tool
def record_payment(
    payer: str, payee: str, amount: str, group: str
) -> dict:
    """Record that `payer` paid `payee` `amount` to settle up (a Splitwise payment)."""
    group_id = _resolve_group(group)
    members = _group(group_id)["members"]
    amt = Decimal(amount).quantize(Decimal("0.01"))
    data = {
        "cost": str(amt),
        "description": "Settle up",
        "group_id": group_id,
        "payment": "true",
        "users__0__user_id": _resolve_member(payer, members),
        "users__0__paid_share": str(amt),
        "users__0__owed_share": "0.00",
        "users__1__user_id": _resolve_member(payee, members),
        "users__1__paid_share": "0.00",
        "users__1__owed_share": str(amt),
    }
    return {"id": _post_expense(data)["id"]}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

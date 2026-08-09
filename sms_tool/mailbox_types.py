from dataclasses import dataclass, field


@dataclass
class MailboxAccount:
    email: str
    password: str = field(default="", repr=False)
    login_password: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    source: str = ""
    provider: str = "graph"
    order_no: str = ""
    token: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    auth_mode: str = ""
    sender_name: str = ""
    seen_message_id: str = ""
    seen_message_ids: tuple[str, ...] = ()
    seen_message_received_ts: int = 0
    purchase_id: str = ""
    project_name: str = ""
    price: str = ""
    purchase_total_cost: str = ""
    balance_after: str = ""

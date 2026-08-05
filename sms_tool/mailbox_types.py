from dataclasses import dataclass


@dataclass
class MailboxAccount:
    email: str
    password: str = ""
    login_password: str = ""
    refresh_token: str = ""
    access_token: str = ""
    source: str = ""
    provider: str = "graph"
    order_no: str = ""
    token: str = ""
    client_secret: str = ""
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

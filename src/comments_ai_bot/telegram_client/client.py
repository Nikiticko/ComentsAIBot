class TelegramAccountClient:
    async def connect(self) -> None:
        raise NotImplementedError

    async def fetch_new_posts(self, channel_username: str) -> list[dict]:
        raise NotImplementedError

    async def can_comment(self, channel_username: str, post_id: int) -> bool:
        raise NotImplementedError

    async def send_comment(self, channel_username: str, post_id: int, text: str) -> int:
        raise NotImplementedError

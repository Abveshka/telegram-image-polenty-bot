import asyncio
import logging
import uuid

import httpx

import config

logger = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"

_client: httpx.AsyncClient | None = None


class YooKassaError(Exception):
    """Ошибка при обращении к API ЮKassa. status_code пуст, если это сбой сети."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if not config.YOOKASSA_SHOP_ID or not config.YOOKASSA_SECRET_KEY:
            raise YooKassaError("Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY в .env")
        _client = httpx.AsyncClient(
            base_url=API_URL,
            auth=(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY),
            timeout=20,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def create_payment(
    *,
    amount_rub: int,
    description: str,
    metadata: dict | None = None,
    return_url: str | None = None,
    save_payment_method: bool = False,
    payment_method_id: str | None = None,
    idempotence_key: str | None = None,
) -> dict:
    body: dict = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,  # списать сразу, без ручного подтверждения
        "description": description[:128],
        "metadata": {k: str(v) for k, v in (metadata or {}).items()},
    }

    if payment_method_id:
        body["payment_method_id"] = payment_method_id
    else:
        body["confirmation"] = {
            "type": "redirect",
            "return_url": return_url or config.YOOKASSA_RETURN_URL,
        }
        if save_payment_method:
            body["save_payment_method"] = True

    headers = {"Idempotence-Key": idempotence_key or str(uuid.uuid4())}

    try:
        resp = await _get_client().post("/payments", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise YooKassaError(f"Сеть: {e!r}") from e

    if resp.status_code >= 400:
        raise YooKassaError(f"create_payment {resp.status_code}: {resp.text}", resp.status_code)
    return resp.json()


async def get_payment(payment_id: str) -> dict:
    try:
        resp = await _get_client().get(f"/payments/{payment_id}")
    except httpx.HTTPError as e:
        raise YooKassaError(f"Сеть: {e!r}") from e

    if resp.status_code >= 400:
        raise YooKassaError(f"get_payment {resp.status_code}: {resp.text}", resp.status_code)
    return resp.json()




async def _demo() -> None:
    payment = await create_payment(
        amount_rub=1,
        description="Тест: первый платёж подписки",
        metadata={"purpose": "test"},
        save_payment_method=True,
    )
    print("id:     ", payment["id"])
    print("status: ", payment["status"])
    print("ссылка: ", payment["confirmation"]["confirmation_url"])
    input("\nОткройте ссылку, оплатите и нажмите Enter...")

    payment = await get_payment(payment["id"])
    print("status: ", payment["status"])
    method = payment.get("payment_method") or {}
    print("способ оплаты сохранён:", method.get("saved"))
    print("payment_method_id:     ", method.get("id"))
    await aclose()


if __name__ == "__main__":
    asyncio.run(_demo())
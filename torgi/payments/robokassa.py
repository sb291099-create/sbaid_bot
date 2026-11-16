"""Robokassa: ссылка на оплату с чеком, проверка уведомления ResultURL, возвраты.

Формат подписей и чеков взят из документации Robokassa; перед запуском
обязательно прогоните оплату и возврат в тестовом режиме магазина.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote, urlencode

log = logging.getLogger(__name__)

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"
REFUND_URL = "https://services.robokassa.ru/RefundService/Refund/Create"


def kop_to_sum(kopecks: int) -> str:
    return f"{Decimal(kopecks) / 100:.2f}"


def sum_to_kop(out_sum: str) -> int:
    return int((Decimal(out_sum) * 100).to_integral_value())


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


@dataclass
class ResultNotification:
    inv_id: int
    amount: int  # копейки


class Robokassa:
    def __init__(self, login: str, password1: str, password2: str, password3: str = "",
                 test_mode: bool = True, sno: str = "usn_income"):
        self.login, self.p1, self.p2, self.p3 = login, password1, password2, password3
        self.test_mode, self.sno = test_mode, sno

    @property
    def configured(self) -> bool:
        return bool(self.login and self.p1 and self.p2)

    def receipt(self, item_name: str, quantity: int, amount_kop: int) -> dict:
        item = {
            "name": item_name[:128],
            "quantity": 1,  # одна позиция «участие в клубе, N акций», сумма — за всю позицию
            "sum": float(Decimal(amount_kop) / 100),
            "payment_method": "full_payment",
            "payment_object": "service",
            "tax": "none",
        }
        r: dict = {"items": [item]}
        if self.sno and self.sno != "npd":  # для самозанятых (Робочеки СМЗ) СНО не передаётся
            r["sno"] = self.sno
        return r

    def payment_url(self, inv_id: int, amount_kop: int, description: str, receipt: dict) -> str:
        out_sum = kop_to_sum(amount_kop)
        receipt_enc = quote(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
        sig = md5(f"{self.login}:{out_sum}:{inv_id}:{receipt_enc}:{self.p1}")
        params = {
            "MerchantLogin": self.login, "OutSum": out_sum, "InvId": inv_id,
            "Description": description[:100], "Receipt": receipt_enc, "SignatureValue": sig,
            "Culture": "ru",
        }
        if self.test_mode:
            params["IsTest"] = 1
        return f"{PAY_URL}?{urlencode(params)}"

    def verify_result(self, params: dict[str, str]) -> ResultNotification | None:
        """Проверка подписи уведомления ResultURL (пароль №2). None — подпись неверна."""
        try:
            out_sum, inv_id, sig = params["OutSum"], params["InvId"], params["SignatureValue"]
        except KeyError:
            return None
        shp = sorted((k, v) for k, v in params.items() if k.lower().startswith("shp_"))
        base = f"{out_sum}:{inv_id}:{self.p2}" + "".join(f":{k}={v}" for k, v in shp)
        if not hmac.compare_digest(md5(base).lower(), sig.lower()):
            return None
        return ResultNotification(inv_id=int(inv_id), amount=sum_to_kop(out_sum))

    # --- возвраты -----------------------------------------------------------------
    async def _op_key(self, http, inv_id: int) -> str:
        sig = md5(f"{self.login}:{inv_id}:{self.p2}")
        async with http.get(OPSTATE_URL, params={"MerchantLogin": self.login, "InvoiceID": inv_id,
                                                 "Signature": sig}) as r:
            body = await r.text()
        m = re.search(r"<OpKey>([^<]+)</OpKey>", body)
        if not m:
            raise RuntimeError(f"Robokassa не вернула OpKey для счёта {inv_id}")
        return m.group(1)

    def _jwt(self, payload: dict) -> str:
        def b64(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = b64(json.dumps(payload, ensure_ascii=False).encode())
        sig = hmac.new(self.p3.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        return f"{header}.{body}.{b64(sig)}"

    async def refund(self, inv_id: int, amount_kop: int, item_name: str) -> str:
        """Создать возврат с чеком возврата. Возвращает id запроса на возврат."""
        if not self.p3:
            raise RuntimeError("ROBOKASSA_PASSWORD3 не задан — автоматический возврат невозможен")
        import aiohttp
        async with aiohttp.ClientSession() as http:
            op_key = await self._op_key(http, inv_id)
            payload = {
                "OpKey": op_key,
                "RefundSum": float(Decimal(amount_kop) / 100),
                "InvoiceItems": [{
                    "Name": item_name[:128], "Quantity": 1, "Cost": float(Decimal(amount_kop) / 100),
                    "Tax": "none", "PaymentMethod": "full_payment", "PaymentObject": "service",
                }],
            }
            async with http.post(REFUND_URL, data=self._jwt(payload),
                                 headers={"Content-Type": "application/json"}) as r:
                data = await r.json(content_type=None)
        if not data or not data.get("success"):
            raise RuntimeError(f"Robokassa отклонила возврат: {data}")
        return str(data.get("requestId", ""))

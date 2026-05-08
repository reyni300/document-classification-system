from datetime import datetime


class ValidationAgent:
    def validate(self, data: dict) -> dict:
        errors = []

        if not self.check_total(data):
            errors.append("Сумма товаров не совпадает с итоговой суммой")

        if not self.check_currency(data):
            errors.append("Неподдерживаемая валюта")

        if not self.check_date(data):
            errors.append("Неверная дата")

        if not self.check_negative_prices(data):
            errors.append("Есть отрицательная цена")

        return {
            "valid": len(errors) == 0,
            "errors": errors
        }

    def check_total(self, data: dict) -> bool:
        items = data.get("items", [])
        total = data.get("total")

        if total is None:
            return False

        if not items:
            return True

        try:
            calc_sum = sum(float(item.get("price", 0)) for item in items if isinstance(item, dict))
            return abs(calc_sum - float(total)) < 0.01
        except Exception:
            return False

    def check_currency(self, data: dict) -> bool:
        currency = str(data.get("currency", "")).upper().strip()
        return currency in ["BYN", "RUB", "USD", "EUR", ""]

    def check_date(self, data: dict) -> bool:
        date_str = str(data.get("date", "")).strip()

        if not date_str or date_str == "N/A" or date_str == "None" or date_str == "":
            return True

        formats = [
            "%d.%m.%Y",
            "%d.%m.%Y %H:%M:%S",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y",
            "%d/%m/%Y %H:%M:%S",
        ]

        for fmt in formats:
            try:
                datetime.strptime(date_str, fmt)
                return True
            except:
                continue
        return False

    def check_negative_prices(self, data: dict) -> bool:
        items = data.get("items", [])
        if not items:
            return True

        try:
            return all(float(item.get("price", 0)) >= 0 for item in items if isinstance(item, dict))
        except Exception:
            return False
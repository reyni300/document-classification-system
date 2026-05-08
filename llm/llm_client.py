import json
import requests


class LLMClient:
    def __init__(self, model="mistral"):
        self.model = model
        self.url = "http://localhost:11434/api/generate"

    def generate(self, prompt: str):
        try:
            response = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json"
                },
                timeout=120
            )

            response.raise_for_status()
            data = response.json()

            if "response" not in data:
                print("ERROR RESPONSE:", data)
                return {"category": "прочее"}

            result = data["response"]
            print("\n[LLM RAW OUTPUT]:", result)

            return self.clean_json(result)

        except Exception as e:
            print("LLM ERROR:", e)
            return {"category": "прочее"}

    def clean_json(self, text: str):
        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")

            if start != -1 and end != -1:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    pass

            return {"category": "прочее"}
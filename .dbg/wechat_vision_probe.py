import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.wechat.vision import WechatVision, PROMPT_CHECK_WECHAT  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-vision-probe.json")
IMG = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-vision-capture.jpg")


def main() -> None:
    v = WechatVision()
    payload = {
        "can_capture_screen": v.can_capture_screen(),
        "model": getattr(v, "_model", ""),
    }
    try:
        image_bytes = v._capture()
        IMG.write_bytes(image_bytes)
        payload["capture_bytes"] = len(image_bytes)
    except Exception as exc:
        payload["capture_error"] = f"{type(exc).__name__}: {exc}"
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    try:
        client = v._get_client()
        b64 = base64.b64encode(image_bytes).decode()
        resp = client.chat.completions.create(
            model=v._model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": PROMPT_CHECK_WECHAT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            max_tokens=200,
        )
        payload["raw_content"] = resp.choices[0].message.content
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        payload["parsed"] = json.loads(text.strip())
    except Exception as exc:
        payload["ask_error"] = f"{type(exc).__name__}: {exc}"

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

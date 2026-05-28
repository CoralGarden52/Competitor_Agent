from __future__ import annotations

import argparse
import base64
import mimetypes
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def to_data_url(image_path: Path) -> str:
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    image_bytes = image_path.read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Test whether current LLM supports image understanding.")
    parser.add_argument(
        "--image",
        default=r"..\PixPin_2026-05-28_15-21-35.png",
        help="Path to local image file (default: ../PixPin_2026-05-28_15-21-35.png)",
    )
    parser.add_argument(
        "--prompt",
        default="说明该图片中石墨文档的定价是多少？",
        help="Question to ask the model.",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model = os.getenv("OPENAI_MODEL")

    if not api_key or not base_url or not model:
        raise RuntimeError("Missing OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL in .env")

    image_path = Path(args.image).resolve()
    image_data_url = to_data_url(image_path)

    client = OpenAI(api_key=api_key, base_url=base_url)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": args.prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ],
        temperature=0,
    )

    answer = response.choices[0].message.content if response.choices else "<no choices>"

    print("=== Vision LLM Test ===")
    print(f"Model: {model}")
    print(f"Image: {image_path}")
    print(f"Prompt: {args.prompt}")
    print("--- LLM Output ---")
    print(answer)


if __name__ == "__main__":
    main()

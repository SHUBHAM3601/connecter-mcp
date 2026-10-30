"""Stand-alone CLIP embedding worker, run in its own Python by ai.py (fastembed needs a Python with
onnxruntime wheels). Usage: python ai_worker.py <request.json> <response.json>

request: {"images": [paths], "texts": [strings], "threads": 2}
response: {"images": [[floats] | null], "texts": [[floats]], "model": "..."}
"""

import json
import sys

IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"


def main() -> None:
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    threads = int(req.get("threads", 2))
    out = {"model": IMAGE_MODEL, "images": [], "texts": []}
    if req.get("images"):
        from fastembed import ImageEmbedding
        from PIL import Image

        model = ImageEmbedding(IMAGE_MODEL, threads=threads)
        for path in req["images"]:
            try:
                with Image.open(path) as im:
                    im.load()
                out["images"].append([round(float(x), 5) for x in next(iter(model.embed([path], batch_size=1)))])
            except Exception:
                out["images"].append(None)
    if req.get("texts"):
        from fastembed import TextEmbedding

        model = TextEmbedding(TEXT_MODEL, threads=threads)
        out["texts"] = [[round(float(x), 5) for x in v] for v in model.embed(req["texts"])]
    json.dump(out, open(sys.argv[2], "w", encoding="utf-8"))


if __name__ == "__main__":
    main()

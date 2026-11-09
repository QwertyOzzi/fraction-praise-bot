"""Re-train the tiny FBI classifier. Offline/dev-only dependency: numpy.

Install once: python -m pip install numpy
Run: python train_tiny_neural.py

Review and add anonymized examples to sample_commands() before retraining.
Do not train on raw Telegram messages, Firebase profiles or secrets.
"""
import base64
import json
import numpy as np
from tiny_neural_ai import FEATURES, HIDDEN, LABELS, _vector, action_features, WEIGHTS_PATH


def sample_commands():
    credit = (
        "дай", "дайте", "выдай", "выдайте", "начисли", "начислите",
        "добавь", "добавьте", "прибавь", "прибавьте",
        "зачисли", "зачислите", "накини", "накинуть",
        "поставь", "поставьте", "присвой", "подкинь"
    )
    debit = (
        "сними", "снимите", "снять", "спиши", "спишите", "списать",
        "вычти", "вычтите", "убери", "уберите", "отними", "отнимите",
        "отнять", "забери", "заберите", "забрать",
        "уменьши", "уменьшите", "оштрафуй", "оштрафовать",
        "сократи", "сократите"
    )
    examples = []
    templates = (
        "{verb} Гоммезу 2 фрп",
        "пожалуйста {verb} Гоммезу три фрп",
        "Гоммезу {verb} 2 похвалы",
        "для Нортона нужно {verb} 3 фрп",
        "можешь {verb} Гоммезу один фрп",
    )
    for label, verbs in (("credit", credit), ("debit", debit)):
        for verb in verbs:
            for template in templates:
                examples.append((template.format(verb=verb), label))
    for text in (
        "как дела", "привет", "когда собрание", "покажи таблицу",
        "не знаю что писать", "пожалуйста помоги", "покажи мой ранг",
        "сколько фрп у Нортона", "оценка работы агента", "доложи новости"
    ):
        examples.append((text, "other"))
    for n in range(60):
        examples.append((f"привет доброе утро какой сегодня план {n} фрп", "other"))
    for verb in ("вычесть", "вычитать", "снизить"):
        for template in (
            "{verb} Нортону 3 фрп", "для Нортона нужно {verb} 2 фрп",
            "Нортону {verb} 3 похвалы", "пожалуйста {verb} Кударову три фрп",
        ):
            examples.append((template.format(verb=verb), "debit"))
    return examples


def main():
    samples = sample_commands()
    X = np.zeros((len(samples), FEATURES), dtype=np.float32)
    Y = np.array([LABELS.index(label) for _, label in samples])
    for row, (text, _) in enumerate(samples):
        for idx, value in _vector(action_features(text)).items():
            X[row, idx] = value
    rng = np.random.default_rng(27)
    weights1 = rng.normal(0, 0.17, (FEATURES, HIDDEN)).astype(np.float32)
    bias1 = np.zeros(HIDDEN, dtype=np.float32)
    weights2 = rng.normal(0, 0.17, (HIDDEN, len(LABELS))).astype(np.float32)
    bias2 = np.zeros(len(LABELS), dtype=np.float32)
    onehot = np.eye(len(LABELS), dtype=np.float32)[Y]
    for _ in range(4000):
        h_pre = X @ weights1 + bias1
        h = np.maximum(0, h_pre)
        scores = h @ weights2 + bias2
        scores -= scores.max(axis=1, keepdims=True)
        p = np.exp(scores)
        p /= p.sum(axis=1, keepdims=True)
        grad = (p - onehot) / len(samples)
        g_w2 = h.T @ grad + 0.0001 * weights2
        g_b2 = grad.sum(axis=0)
        g_h = (grad @ weights2.T) * (h_pre > 0)
        g_w1 = X.T @ g_h + 0.0001 * weights1
        g_b1 = g_h.sum(axis=0)
        weights1 -= 0.095 * g_w1
        bias1 -= 0.095 * g_b1
        weights2 -= 0.095 * g_w2
        bias2 -= 0.095 * g_b2

    def pack(values):
        flat = np.array(values).reshape(-1)
        scale = max(float(np.max(np.abs(flat))) / 127.0, 1e-7)
        signed = np.clip(np.round(flat / scale), -127, 127).astype(np.int8)
        return base64.b64encode(signed.tobytes()).decode("ascii"), scale

    model = {"version": 2, "features": FEATURES, "hidden": HIDDEN, "labels": list(LABELS)}
    for name, tensor in (("w1", weights1), ("b1", bias1),
                         ("w2", weights2), ("b2", bias2)):
        model[name], model["scale_" + name] = pack(tensor)
    WEIGHTS_PATH.write_text(
        json.dumps(model, ensure_ascii=False, separators=(",", ":")), "utf-8"
    )
    preds = np.argmax(np.maximum(0, X @ weights1 + bias1) @ weights2 + bias2, axis=1)
    print(f"Trained {len(samples)} synthetic examples; training accuracy "
          f"{(preds == Y).mean():.1%}.")
    print("Important: training accuracy is NOT held-out accuracy; test new commands.")


if __name__ == "__main__":
    main()

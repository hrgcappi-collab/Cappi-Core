"""Мелкая линейная алгебра: МНК с двусторонними эффектами и кластерной ошибкой."""
import math


def обратить(A):
    n = len(A)
    M = [list(строка) + [1.0 if i == j else 0.0 for j in range(n)]
         for i, строка in enumerate(A)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[p][i]) < 1e-12:
            raise ValueError("матрица вырождена")
        M[i], M[p] = M[p], M[i]
        д = M[i][i]
        M[i] = [x / д for x in M[i]]
        for r in range(n):
            if r != i and M[r][i]:
                k = M[r][i]
                M[r] = [a - k * b for a, b in zip(M[r], M[i])]
    return [строка[n:] for строка in M]


def внутри(y, X, группы, веса=None):
    """Убираем эффекты блюда и недели попеременным вычитанием средних."""
    n = len(y)
    веса = веса or [1.0] * n
    столбцы = [list(c) for c in zip(*X)] if X else []
    ряды = [list(y)] + столбцы
    for _ in range(60):
        сдвиг = 0.0
        for g in группы:
            суммы = {}
            for i, к in enumerate(g):
                s = суммы.setdefault(к, [0.0] * (len(ряды) + 1))
                for j, ряд in enumerate(ряды):
                    s[j] += ряд[i] * веса[i]
                s[-1] += веса[i]
            for i, к in enumerate(g):
                s = суммы[к]
                for j, ряд in enumerate(ряды):
                    m = s[j] / s[-1]
                    сдвиг = max(сдвиг, abs(m))
                    ряд[i] -= m
        if сдвиг < 1e-10:
            break
    return ряды[0], [list(c) for c in zip(*ряды[1:])] if len(ряды) > 1 else [[] for _ in y]


def мнк(y, X, кластер, веса=None, потеряно=0):
    """Оценка, кластерная ошибка по блюду, R² внутри."""
    n, k = len(y), len(X[0])
    веса = веса or [1.0] * n
    XtX = [[sum(веса[i] * X[i][a] * X[i][b] for i in range(n)) for b in range(k)]
           for a in range(k)]
    Xty = [sum(веса[i] * X[i][a] * y[i] for i in range(n)) for a in range(k)]
    inv = обратить(XtX)
    beta = [sum(inv[a][b] * Xty[b] for b in range(k)) for a in range(k)]
    u = [y[i] - sum(beta[a] * X[i][a] for a in range(k)) for i in range(n)]
    # Кластерный сэндвич: наблюдения одного блюда связаны между собой.
    мешок = {}
    for i in range(n):
        s = мешок.setdefault(кластер[i], [0.0] * k)
        for a in range(k):
            s[a] += веса[i] * X[i][a] * u[i]
    G = len(мешок)
    meat = [[sum(v[a] * v[b] for v in мешок.values()) for b in range(k)] for a in range(k)]
    попр = G / max(G - 1, 1) * (n - 1) / max(n - k - потеряно, 1)
    V = [[попр * sum(inv[a][c] * meat[c][d] * inv[d][b]
                     for c in range(k) for d in range(k)) for b in range(k)]
         for a in range(k)]
    se = [math.sqrt(max(V[a][a], 0)) for a in range(k)]
    ssr = sum(веса[i] * u[i] ** 2 for i in range(n))
    sst = sum(веса[i] * y[i] ** 2 for i in range(n))
    return {"beta": beta, "se": se, "n": n, "групп": G,
            "R2": 1 - ssr / sst if sst else 0.0}

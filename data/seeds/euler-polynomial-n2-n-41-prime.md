---
title: "n^2 + n + 41 is prime for all non-negative integers n"
field: number-theory
origin: user
tractability: 1
---

## Statement

For every non-negative integer $n$ (i.e. $n \in \mathbb{Z}$, $n \ge 0$), the value $n^2 + n + 41$ is a prime number.

## Triage

- Literature check: this is Euler's famous prime-producing polynomial (Euler lucky number 41; see ProofWiki "Euler Lucky Number/Examples/41" and The Mathematical Gazette, "Euler's prime-producing polynomial revisited"). It is known to produce primes exactly for $0 \le n \le 39$ and to fail at $n = 40$.
- Small-case computation (2026-08-07, direct primality test): $n^2+n+41$ is prime for all $n = 0,\dots,39$.
- **Counterexample**: $n = 40$ gives $40^2 + 40 + 41 = 1681 = 41^2$, which is composite. The statement is therefore FALSE. (Also composite at $n = 41$: $1763 = 41 \cdot 43$.)
- Routing: filed with status `falsified`; no solver dispatched.

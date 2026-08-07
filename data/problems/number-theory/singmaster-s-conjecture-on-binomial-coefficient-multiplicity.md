---
id: "number-theory/singmaster-s-conjecture-on-binomial-coefficient-multiplicity"
title: "Singmaster's conjecture on binomial coefficient multiplicity"
field: "number-theory"
origin: "classic"
status: "queued"
tractability: "3"
added_at_utc: "2026-08-07T12:43:57.561243+00:00"
---

## Statement

Singmaster's conjecture (1971): for an integer $a > 1$, define its binomial multiplicity $N(a) = \#\{(n, k) : 0 \le k \le n,\ \binom{n}{k} = a\}$, i.e. the number of times $a$ appears as an entry of Pascal's triangle. The conjecture asserts that $N(a)$ is bounded by an absolute constant: there exists $C$ such that $N(a) \le C$ for every $a > 1$. The largest known value is $N(3003) = 8$. Best unconditional upper bounds are of the shape $N(a) = O(\log a)$ up to iterated-logarithm factors (Kane, 2007, and subsequent refinements), very far from the conjectured $O(1)$.

## Context

Asks how often a single number can recur in Pascal's triangle — a natural Diophantine question about the equation $\binom{n}{k} = \binom{m}{j}$ whose resolution would sharpen our understanding of binomial coefficients as values of a two-variable arithmetic function; concrete, checkable, and supported by only logarithmic-strength partial results.

## Known partial results

Search on 2026-08-07: Singmaster's conjecture is listed as open both on Wikipedia and in the Open Problem Garden; no accepted resolution or counterexample appears in the search results, only partial upper bounds.

## References

- https://en.wikipedia.org/wiki/Singmaster%27s_conjecture
- https://www.openproblemgarden.org/op/singmasters_conjecture

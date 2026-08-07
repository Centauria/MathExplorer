# Proof blueprint: sum of the first n odd numbers

# theorem thm:odd-sum

## statement
Prove that $\sum_{k=1}^{n}(2k-1) = n^2$ for every integer $n \ge 1$.

## proof
We proceed by mathematical induction on $n$.

**Base case ($n = 1$).** The left-hand side is the single summand
$\sum_{k=1}^{1}(2k-1) = 2\cdot 1 - 1 = 1$, and the right-hand side is
$1^2 = 1$. Hence the identity holds for $n = 1$.

**Induction step.** Assume the identity holds for some integer $n \ge 1$,
that is,
$$\sum_{k=1}^{n}(2k-1) = n^2.$$
Then for $n+1$ we compute
$$\sum_{k=1}^{n+1}(2k-1)
= \sum_{k=1}^{n}(2k-1) + \bigl(2(n+1)-1\bigr)
= n^2 + (2n + 1)
= (n+1)^2,$$
where the second equality uses the induction hypothesis and the last
equality is the binomial expansion $(n+1)^2 = n^2 + 2n + 1$.

By the principle of mathematical induction, the identity
$\sum_{k=1}^{n}(2k-1) = n^2$ holds for every integer $n \ge 1$. $\blacksquare$

**Remark (independent check).** The same identity follows directly from the
closed form for the sum of the first $n$ positive integers:
$\sum_{k=1}^{n}(2k-1) = 2\sum_{k=1}^{n} k - n = 2\cdot\frac{n(n+1)}{2} - n
= n(n+1) - n = n^2$.

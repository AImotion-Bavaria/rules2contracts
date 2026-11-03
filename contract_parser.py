#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Textual contract syntax + parser — the LLM output format.

An LLM (or a human) writes rule contracts as a YAML document; formulas are
given in a small STL DSL that is parsed into the checker's AST
(`stl_ag_checker`). The grammar is exactly the decidable fragment fixed on
2026-08-07 — anything outside it is a parse error, so ill-formed LLM output
is rejected *syntactically* before any semantic check runs.

Formula grammar (EBNF):

    contract  := 'G' '(' formula ')'                  # outermost G mandatory
    formula   := disj ( '->' formula )?               # implication, right-assoc
    disj      := conj ( ('or'|'|') conj )*
    conj      := unary ( ('and'|'&') unary )*
    unary     := ('not'|'!') unary
               | ('F'|'H'|'O') '[' INT ',' INT ']' '(' formula ')'
               | primary
    primary   := comparison | BOOL_SIGNAL | '(' formula ')'
    comparison:= arith CMP arith                      # CMP: <= >= < > == !=
    arith     := term ( ('+'|'-') term )*
    term      := factor ( ('*'|'/') factor )*         # linear only (see below)
    factor    := NUMBER | CONSTANT | SIGNAL
               | 'abs' '(' arith ')' | '-' factor | '(' arith ')'

Linearity is enforced: '*' requires at least one constant operand, '/' a
constant divisor. `abs(e)` is encoded as if-then-else (stays in QF_LRA).
Temporal bounds are integers (steps at the declared sampling rate).
"""

from __future__ import annotations
import re
import z3

from stl_ag_checker import (Signal, Atom, Not, And, Or, Implies, F, Hist,
                            Once, G, Formula)


class ContractSyntaxError(Exception):
    pass


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"""
    (?P<num>\d+\.\d+|\d+)
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>->|<=|>=|==|!=|<|>|\+|-|\*|/|\(|\)|\[|\]|,|&|\||!)
  | (?P<ws>\s+)
""", re.VERBOSE)


def tokenize(text: str) -> list[str]:
    tokens, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise ContractSyntaxError(
                f"illegal character {text[pos]!r} at position {pos} in: {text}")
        if m.lastgroup != "ws":
            tokens.append(m.group())
        pos = m.end()
    return tokens


# ---------------------------------------------------------------------------
# Arithmetic expressions (closures over z3 variables)
# ---------------------------------------------------------------------------

class Arith:
    """Arithmetic expression: z3 closure + used signals + linear normal form.

    `lin` maps a base to its coefficient; bases are None (the constant 1),
    a signal name, or ('abs', <canonical inner string>) for abs(...) terms.
    Linearity of the fragment guarantees this form always exists."""

    def __init__(self, fn, signals: set[str], has_signal: bool,
                 lin: dict | None = None,
                 is_bare_signal: str | None = None):
        self.fn = fn                      # dict(name -> z3 var) -> z3 expr
        self.signals = signals
        self.has_signal = has_signal
        self.lin = lin if lin is not None else {}
        self.is_bare_signal = is_bare_signal


# Side table: canonical string of an abs(...) argument -> its linear form.
# Populated at parse time; lets numeric backends evaluate abs terms without
# re-parsing the canonical string.
ABS_INNER: dict[str, dict] = {}


def lin_str(lin: dict) -> str:
    """Canonical, parenthesis-free rendering of a linear form
    (e.g. for the rtamt backend): 'c1 * s1 - c2 * s2 + c0'.
    Negative coefficients are rendered via binary subtraction — some STL
    grammars (rtamt) silently mis-handle '+ -c * x'."""
    parts: list[tuple[float, str]] = []
    for base in sorted((b for b in lin if b is not None),
                       key=lambda b: b[1] if isinstance(b, tuple) else b):
        coeff = lin[base]
        if coeff == 0:
            continue
        name = f"abs({base[1]})" if isinstance(base, tuple) else base
        parts.append((coeff, name))
    const = lin.get(None, 0.0)
    if const != 0.0 or not parts:
        parts.append((const if parts or const else 0.0, None))
    # put a positive term first if one exists (avoids a leading unary minus)
    parts.sort(key=lambda p: 0 if p[0] > 0 else 1)
    out = ""
    for i, (coeff, name) in enumerate(parts):
        mag = abs(coeff)
        term = repr(float(mag)) if name is None else \
            (name if mag == 1.0 else f"{mag!r} * {name}")
        if i == 0:
            out = term if coeff >= 0 else f"0.0 - {term}"
        else:
            out += (" + " if coeff >= 0 else " - ") + term
    return out


def _lin_scale(lin: dict, factor: float) -> dict:
    return {k: v * factor for k, v in lin.items()}


def _lin_add(a: dict, b: dict, sign: float = 1.0) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + sign * v
    return out


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

CMP_OPS = {"<=", ">=", "<", ">", "==", "!="}


class Parser:
    def __init__(self, text: str, signals: dict[str, Signal],
                 constants: dict[str, float]):
        self.text = text
        self.toks = tokenize(text)
        self.pos = 0
        self.signals = signals
        self.constants = constants

    # -- token helpers ------------------------------------------------------

    def peek(self, k: int = 0) -> str | None:
        return self.toks[self.pos + k] if self.pos + k < len(self.toks) else None

    def next(self) -> str:
        if self.pos >= len(self.toks):
            raise ContractSyntaxError(f"unexpected end of formula: {self.text}")
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def _bound(self) -> int:
        """Temporal bounds must be integer step literals; anything else
        (e.g. a named constant such as F[0,RESPONSE_STEPS]) is a syntax
        error of the fragment, not a crash."""
        tok = self.next()
        try:
            return int(tok)
        except (TypeError, ValueError):
            raise ContractSyntaxError(
                f"temporal bound {tok!r} is not an integer step literal — "
                f"the fragment requires numeric bounds, e.g. F[0,3]"
                f" in: {self.text}") from None

    def expect(self, tok: str):
        got = self.next()
        if got != tok:
            raise ContractSyntaxError(
                f"expected {tok!r}, got {got!r} in: {self.text}")

    # -- entry point --------------------------------------------------------

    def parse_contract_formula(self) -> G:
        self.expect("G")
        self.expect("(")
        phi = self.parse_formula()
        self.expect(")")
        if self.pos != len(self.toks):
            raise ContractSyntaxError(
                f"trailing tokens {self.toks[self.pos:]} in: {self.text}")
        return G(phi)

    # -- boolean layer ------------------------------------------------------

    def parse_formula(self) -> Formula:
        lhs = self.parse_disj()
        if self.peek() == "->":
            self.next()
            return Implies(lhs, self.parse_formula())
        return lhs

    def parse_disj(self) -> Formula:
        parts = [self.parse_conj()]
        while self.peek() in ("or", "|"):
            self.next()
            parts.append(self.parse_conj())
        return parts[0] if len(parts) == 1 else Or(*parts)

    def parse_conj(self) -> Formula:
        parts = [self.parse_unary()]
        while self.peek() in ("and", "&"):
            self.next()
            parts.append(self.parse_unary())
        return parts[0] if len(parts) == 1 else And(*parts)

    def parse_unary(self) -> Formula:
        tok = self.peek()
        if tok in ("not", "!"):
            self.next()
            return Not(self.parse_unary())
        if tok in ("F", "H", "O") and self.peek(1) == "[":
            op = self.next()
            self.expect("[")
            a = self._bound()
            self.expect(",")
            b = self._bound()
            self.expect("]")
            self.expect("(")
            phi = self.parse_formula()
            self.expect(")")
            cls = {"F": F, "H": Hist, "O": Once}[op]
            return cls(a, b, phi)
        if tok == "G":
            raise ContractSyntaxError(
                "nested G is outside the fragment (G only outermost): "
                f"{self.text}")
        return self.parse_primary()

    def parse_primary(self) -> Formula:
        # try: comparison or bare boolean signal (backtrack on failure)
        save = self.pos
        try:
            lhs = self.parse_arith()
            if self.peek() in CMP_OPS:
                op = self.next()
                rhs = self.parse_arith()
                return self.make_comparison(lhs, op, rhs, start=save)
            name = lhs.is_bare_signal
            if name is not None and self.signals[name].sort == "bool":
                return Atom(lambda v, n=name: v[n], {self.signals[name]}, name)
            raise ContractSyntaxError("not an atom")
        except ContractSyntaxError:
            self.pos = save
        # fallback: parenthesized boolean formula
        self.expect("(")
        phi = self.parse_formula()
        self.expect(")")
        return phi

    def make_comparison(self, lhs: Arith, op: str, rhs: Arith,
                        start: int) -> Atom:
        ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
               "<": lambda a, b: a < b, ">": lambda a, b: a > b,
               "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
        names = lhs.signals | rhs.signals
        sigs = {self.signals[n] for n in names}
        label = " ".join(self.toks[start:self.pos])
        fn = (lambda v, l=lhs.fn, r=rhs.fn, o=ops[op]: o(l(v), r(v)))
        atom = Atom(fn, sigs, label)
        atom.linear = (lhs.lin, op, rhs.lin)  # for backend translation
        return atom

    # -- arithmetic layer ---------------------------------------------------

    def parse_arith(self) -> Arith:
        e = self.parse_term()
        while self.peek() in ("+", "-"):
            op = self.next()
            r = self.parse_term()
            f = (lambda v, l=e.fn, r_=r.fn: l(v) + r_(v)) if op == "+" else \
                (lambda v, l=e.fn, r_=r.fn: l(v) - r_(v))
            e = Arith(f, e.signals | r.signals, e.has_signal or r.has_signal,
                      _lin_add(e.lin, r.lin, 1.0 if op == "+" else -1.0))
        return e

    def parse_term(self) -> Arith:
        e = self.parse_factor()
        while self.peek() in ("*", "/"):
            op = self.next()
            r = self.parse_factor()
            if op == "*" and e.has_signal and r.has_signal:
                raise ContractSyntaxError(
                    f"nonlinear term (signal * signal) outside the "
                    f"fragment: {self.text}")
            if op == "/" and r.has_signal:
                raise ContractSyntaxError(
                    f"division by a signal outside the fragment: {self.text}")
            if op == "*":
                f = lambda v, l=e.fn, r_=r.fn: l(v) * r_(v)
                lin = _lin_scale(e.lin, r.lin.get(None, 0.0)) \
                    if not e.has_signal and r.has_signal is False \
                    else (_lin_scale(r.lin, e.lin.get(None, 0.0))
                          if not e.has_signal else
                          _lin_scale(e.lin, r.lin.get(None, 0.0)))
            else:
                f = lambda v, l=e.fn, r_=r.fn: l(v) / r_(v)
                lin = _lin_scale(e.lin, 1.0 / r.lin.get(None, 1.0))
            e = Arith(f, e.signals | r.signals,
                      e.has_signal or r.has_signal, lin)
        return e

    def parse_factor(self) -> Arith:
        tok = self.peek()
        if tok is None:
            raise ContractSyntaxError(f"unexpected end of formula: {self.text}")
        if tok == "-":
            self.next()
            e = self.parse_factor()
            return Arith(lambda v, f=e.fn: -f(v), e.signals, e.has_signal,
                         _lin_scale(e.lin, -1.0))
        if tok == "(":
            self.next()
            e = self.parse_arith()
            self.expect(")")
            return Arith(e.fn, e.signals, e.has_signal, e.lin)
        if tok == "abs":
            self.next()
            self.expect("(")
            e = self.parse_arith()
            self.expect(")")
            # Record the inner linear form under its canonical string so that
            # numeric backends (monitor_backend) can evaluate abs terms
            # without re-parsing; the lin key stays a hashable string pair.
            ABS_INNER[lin_str(e.lin)] = e.lin
            return Arith(
                lambda v, f=e.fn: z3.If(f(v) >= 0, f(v), -f(v)),
                e.signals, e.has_signal,
                {("abs", lin_str(e.lin)): 1.0})
        if re.fullmatch(r"\d+\.\d+|\d+", tok):
            self.next()
            val = float(tok)
            return Arith(lambda v, c=val: z3.RealVal(c), set(), False,
                         {None: val})
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", tok):
            self.next()
            if tok in self.constants:
                val = float(self.constants[tok])
                return Arith(lambda v, c=val: z3.RealVal(c), set(), False,
                             {None: val})
            if tok in self.signals:
                return Arith(lambda v, n=tok: v[n], {tok}, True,
                             {tok: 1.0}, is_bare_signal=tok)
            raise ContractSyntaxError(
                f"unknown identifier {tok!r} (not a declared signal or "
                f"constant) in: {self.text}")
        raise ContractSyntaxError(f"unexpected token {tok!r} in: {self.text}")


def parse_contract_formula(text: str, signals: dict[str, Signal],
                           constants: dict[str, float]) -> G:
    """Parse a contract formula (outermost G mandatory) into the checker AST."""
    return Parser(text, signals, constants).parse_contract_formula()

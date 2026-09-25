#!/usr/bin/env python3
"""labelquery - the boolean label query behind `-Queue 'where: <query>'` (#38). Pure: no I/O.

  bug AND priority IN [P0, P1] AND NOT wontfix
  (bug OR follow-up) AND priority NOT IN [P2, P3]
  label IN [bug, regression] AND NOT "needs design"

Grammar, precedence NOT > AND > OR, parentheses override it:
  expr  := or
  or    := and (OR and)*
  and   := unary (AND unary)*
  unary := NOT unary | atom
  atom  := '(' expr ')' | LABEL | KEY IN '[' list ']' | KEY NOT IN '[' list ']'
  list  := value (',' value)*

- Keywords (AND, OR, NOT, IN) are case-insensitive; a label spelled like one must be quoted. There
  is no implicit AND: `bug wontfix` is an error.
- A bare label is letters, digits and `: - _ . /`; anything else (spaces, `c++`, emoji, brackets)
  is quoted with "..." or '...', where a backslash escapes the next character.
- `KEY IN [a, b]` means the label `KEY:a` or `KEY:b` (exactly, case-insensitively); the key `label`
  means the bare names. `NOT IN` is its negation, so it also matches issues with no such label.
- Matching is case-insensitive, like GitHub labels.
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = {'and', 'or', 'not', 'in'}
BARE_EXTRA = set(':-_./')


class QueryError(ValueError):
    def __init__(self, text: str, column: int, message: str):
        self.text, self.column, self.message = text, column, message
        super().__init__(f'column {column}: {message}\n  {text}\n  {" " * (column - 1)}^')


@dataclass(frozen=True)
class Label:
    name: str


@dataclass(frozen=True)
class In:
    key: str
    values: tuple
    negated: bool = False


@dataclass(frozen=True)
class Not:
    item: object


@dataclass(frozen=True)
class And:
    items: tuple


@dataclass(frozen=True)
class Or:
    items: tuple


@dataclass(frozen=True)
class Query:
    """A parsed query as the queue keeps it: `text` for display, `key` for identity."""
    text: str
    key: str

    def __bool__(self):
        return True


@dataclass(frozen=True)
class Token:
    kind: str          # 'word', 'keyword', '(', ')', '[', ']', ',', 'end'
    value: str
    column: int
    quoted: bool = False


def bare(ch: str) -> bool:
    return ch.isalnum() or ch in BARE_EXTRA


def tokenize(text: str) -> list[Token]:
    tokens, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch in '()[],':
            tokens.append(Token(ch, ch, i + 1))
            i += 1
        elif ch in '"\'':
            start, i, value = i, i + 1, []
            while i < len(text) and text[i] != ch:
                if text[i] == '\\':
                    i += 1
                    if i >= len(text):
                        break
                value.append(text[i])
                i += 1
            if i >= len(text):
                raise QueryError(text, start + 1, f'unterminated quote {ch}')
            i += 1
            tokens.append(Token('word', ''.join(value), start + 1, quoted=True))
        elif bare(ch):
            start = i
            while i < len(text) and bare(text[i]):
                i += 1
            word = text[start:i]
            kind = 'keyword' if word.casefold() in KEYWORDS else 'word'
            tokens.append(Token(kind, word.casefold() if kind == 'keyword' else word, start + 1))
        else:
            raise QueryError(text, i + 1, f"unexpected character {ch!r}; quote a label with special characters")
    tokens.append(Token('end', '', len(text) + 1))
    return tokens


def describe(token: Token) -> str:
    if token.kind == 'end':
        return 'the end of the query'
    if token.kind == 'keyword':
        return token.value.upper()
    return repr(token.value) if token.kind == 'word' else f"'{token.value}'"


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    def peek(self, ahead=0) -> Token:
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def keyword(self, word, ahead=0) -> bool:
        token = self.peek(ahead)
        return token.kind == 'keyword' and token.value == word

    def fail(self, expected: str):
        token = self.peek()
        raise QueryError(self.text, token.column, f'expected {expected}, found {describe(token)}')

    def take(self, kind: str, expected: str) -> Token:
        if self.peek().kind != kind:
            self.fail(expected)
        self.i += 1
        return self.tokens[self.i - 1]

    def parse(self):
        if self.peek().kind == 'end':
            self.fail('an expression')
        node = self.or_()
        if self.peek().kind != 'end':
            self.fail("AND, OR or the end of the query" + (" (')' has no matching '(')" if self.peek().kind == ')' else ''))
        return node

    def or_(self):
        items = [self.and_()]
        while self.keyword('or'):
            self.i += 1
            items.append(self.and_())
        return items[0] if len(items) == 1 else Or(tuple(items))

    def and_(self):
        items = [self.unary()]
        while self.keyword('and'):
            self.i += 1
            items.append(self.unary())
        return items[0] if len(items) == 1 else And(tuple(items))

    def unary(self):
        if self.keyword('not'):
            self.i += 1
            return Not(self.unary())
        return self.atom()

    def atom(self):
        token = self.peek()
        if token.kind == '(':
            self.i += 1
            node = self.or_()
            self.take(')', "AND, OR or ')'")
            return node
        if token.kind != 'word':
            self.fail("a label, NOT or '('")
        self.i += 1
        # Two tokens of lookahead: `KEY IN [` and `KEY NOT IN [` make a membership test.
        if self.keyword('in'):
            self.i += 1
            return In(token.value, self.values(), False)
        if self.keyword('not') and self.keyword('in', 1):
            self.i += 2
            return In(token.value, self.values(), True)
        return Label(token.value)

    def values(self) -> tuple:
        self.take('[', "'['")
        items = [self.take('word', 'a value (the list may not be empty)').value]
        while self.peek().kind == ',':
            self.i += 1
            items.append(self.take('word', "a value after ','").value)
        self.take(']', "',' or ']'")
        return tuple(items)


def parse(text: str):
    return Parser(text.strip()).parse()


def labels_of(names) -> set[str]:
    return {str(name).casefold() for name in names}


def evaluate(node, labels: set[str]) -> bool:
    """`labels` casefolded (labels_of)."""
    if isinstance(node, Label):
        return node.name.casefold() in labels
    if isinstance(node, In):
        if node.key.casefold() == 'label':
            wanted = {value.casefold() for value in node.values}
        else:
            wanted = {f'{node.key}:{value}'.casefold() for value in node.values}
        return bool(wanted & labels) != node.negated
    if isinstance(node, Not):
        return not evaluate(node.item, labels)
    if isinstance(node, And):
        return all(evaluate(item, labels) for item in node.items)
    if isinstance(node, Or):
        return any(evaluate(item, labels) for item in node.items)
    raise TypeError(node)


def quote(word: str) -> str:
    if word and all(bare(ch) for ch in word) and word.casefold() not in KEYWORDS:
        return word
    return '"' + word.replace('\\', '\\\\').replace('"', '\\"') + '"'


def canonical(node) -> str:
    """Fully parenthesised, keywords upper-case, labels quoted only when needed, as written
    otherwise. parse(canonical(x)) == x."""
    if isinstance(node, Label):
        return quote(node.name)
    if isinstance(node, In):
        return f"{quote(node.key)} {'NOT IN' if node.negated else 'IN'} [" + ', '.join(quote(v) for v in node.values) + ']'
    if isinstance(node, Not):
        return 'NOT ' + canonical(node.item)
    if isinstance(node, (And, Or)):
        joiner = ' AND ' if isinstance(node, And) else ' OR '
        return '(' + joiner.join(canonical(item) for item in node.items) + ')'
    raise TypeError(node)


def flatten(node):
    """Nested groups of the same operator become one: (a AND (b AND c)) == (a AND b AND c)."""
    if isinstance(node, Not):
        return Not(flatten(node.item))
    if isinstance(node, (And, Or)):
        items = []
        for item in (flatten(child) for child in node.items):
            items.extend(item.items if type(item) is type(node) else [item])
        return type(node)(tuple(items))
    return node


def normalised(node) -> str:
    """The identity of a query: the same logic in another spelling (case, spacing, quoting,
    redundant parentheses) gives the same string. Semantic equivalence is out of scope."""
    return canonical(flatten(node)).casefold()


def compile_query(text: str) -> tuple[object, Query]:
    node = parse(text)
    return node, Query(canonical(node), normalised(node))

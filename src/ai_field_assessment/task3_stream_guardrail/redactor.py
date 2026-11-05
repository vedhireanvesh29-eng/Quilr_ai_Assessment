"""Conservative lexical buffering, independent of transport chunk boundaries.

Whitespace terminates ordinary candidates. Numeric runs include spaces/hyphens
so formatted cards survive token boundaries. Oversized candidates are replaced
once, and their remainder is discarded through the next safe delimiter.
"""

import re

MARKER = "[REDACTED]"
EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
SSN = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
CARD = re.compile(r"(?<![0-9])(?:[0-9][\s-]*){12,18}[0-9](?![0-9])")


def luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isascii() and c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def redact(value: str) -> str:
    if "@" in value:
        value = EMAIL.sub(MARKER, value)
        # Also hide incomplete email-like tokens rather than releasing a prefix at EOF.
        if "@" in value:
            return MARKER
    value = SSN.sub(MARKER, value)
    return CARD.sub(lambda m: MARKER if luhn(m[0]) else m[0], value)


class StreamingRedactor:
    def __init__(self, max_buffer: int = 4096) -> None:
        if max_buffer < 32:
            raise ValueError("max_buffer must be at least 32")
        self.max_buffer = max_buffer
        self.pending = ""
        self.numeric = False
        self.dropping = False
        self.high_water = 0
        self.drop_gap = False

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            digit = char in "0123456789"
            number_char = digit or char == "-" or char.isspace()
            if self.dropping:
                # An oversized token may end in the first group of a card.
                # Suppress following numeric groups as well as the token itself.
                if char.isspace():
                    self.drop_gap = True
                    continue
                if not self.drop_gap or digit or char == "-":
                    continue
                self.dropping = False
                self.drop_gap = False
                self.numeric = False
            if self.numeric and not number_char:
                if any(c.isspace() for c in self.pending):
                    output.append(redact(self.pending))
                    self.pending = ""
                self.numeric = False
            if char.isspace() and not self.numeric:
                # Prefixes and punctuation do not terminate a trailing numeric
                # candidate: card=4111 1111 ... must remain one candidate.
                if "@" not in self.pending and re.search(r"[0-9][0-9-]*$", self.pending):
                    self.numeric = True
                else:
                    output.append(redact(self.pending))
                    output.append(char)
                    self.pending = ""
                    continue
            if not self.pending:
                self.numeric = digit
            self.pending += char
            self.high_water = max(self.high_water, len(self.pending))
            if len(self.pending) == self.max_buffer:
                output.append(MARKER)
                self.pending = ""
                self.dropping = True
                self.drop_gap = False
        return "".join(output)

    def finish(self) -> str:
        output = redact(self.pending)
        output = re.sub(r"(?<![0-9])[0-9]{3}-[0-9]{0,2}(?:-[0-9]{0,3})?$", MARKER, output)
        self.pending = ""
        self.numeric = False
        self.dropping = False
        self.drop_gap = False
        return output

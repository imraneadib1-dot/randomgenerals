"""A small Flet calculator.

Fixed version of the file that was open (unsaved) in the editor. What
was actually wrong with the original, all verified against the
installed Flet 0.86.5 rather than guessed at:

1. `ft.app(target=main)` is deprecated as of Flet 0.80 - it still runs
   but warns. `ft.run(main)` is the current call.
2. `bgcolor="surfaceVariant"` referenced a colour that no longer exists
   in this version (Material 3 dropped it); `hasattr(ft.Colors,
   "SURFACE_VARIANT")` is False here. Buttons using it had no valid
   background. Replaced with SURFACE_CONTAINER_HIGHEST.
3. The bottom row had two "=" buttons - `btn("="), btn("=", ...)`. The
   third slot is now a backspace, which is what the layout was missing.
4. `eval()` on the raw display string had a real, reachable bug, not
   just a style problem: pressing 1 + 0 7 builds "1+07", and Python
   rejects leading zeros in integer literals, so a valid sum returned
   "Error". Fixed at both ends - input no longer produces leading
   zeros, and evaluation no longer uses eval() at all.
5. Theme mode and colours now use the real enums instead of loose
   strings, so a typo fails loudly at import instead of silently
   rendering wrong.

The evaluator below walks a parsed AST and permits only numbers and the
five arithmetic operators. Button-only input meant eval() wasn't
practically exploitable here, but an expression evaluator that can't
execute arbitrary code regardless of how it's fed is worth the ~20
lines - not least because it also lets the calculator give a proper
"Error" for division by zero instead of crashing.
"""
import ast
import operator

import flet as ft

# Only these node types are allowed through the evaluator. Anything else
# in the parsed tree (a call, a name, an attribute lookup) is rejected.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expression):
    """Evaluate a arithmetic expression -> float. Raises ValueError on
    anything that isn't plain arithmetic, and lets ZeroDivisionError
    through for the caller to report."""
    tree = ast.parse(expression, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)
            ):
                raise ValueError("only numbers are allowed")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](walk(node.operand))
        raise ValueError("unsupported expression")

    return walk(tree)


def format_result(value):
    """2.0 -> "2", 2.5 -> "2.5". Avoids showing a trailing .0 on whole
    numbers without turning 2.5 into 2."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "Error"
        if value.is_integer():
            return str(int(value))
        # Trim floating-point noise: 0.1 + 0.2 should read 0.3, not
        # 0.30000000000000004.
        return f"{round(value, 10):g}"
    return str(value)


def trailing_number(text):
    """The number currently being typed, i.e. the digits/point at the end
    of the expression. Lets input tell '0' (replace it) from '10'
    (append to it)."""
    buf = ""
    for ch in reversed(text):
        if ch.isdigit() or ch == ".":
            buf = ch + buf
        else:
            break
    return buf


def apply_key(current, key):
    """current display string + a key -> new display string.

    Deliberately a pure function rather than a closure over the Flet
    control: all the fiddly input rules (leading zeros, stacked
    operators, stray decimal points) live here and can be tested
    without constructing a page.
    """
    if key == "C":
        return "0"
    if key == "back":
        return current[:-1] or "0"
    if key == "=":
        try:
            return format_result(safe_eval(current))
        except (ZeroDivisionError, ValueError, SyntaxError):
            return "Error"

    if current == "Error":
        current = "0"

    if key.isdigit():
        # Replace a lone zero rather than appending to it - this is what
        # stops "07" (which Python's parser rejects) being built at all.
        if trailing_number(current) == "0":
            return current[:-1] + key
        return current + key

    if key == ".":
        trailing = trailing_number(current)
        if "." in trailing:
            return current          # already a point in this number
        return current + ("." if trailing else "0.")

    if key in "+-*/":
        if current and current[-1] in "+-*/":
            return current[:-1] + key   # swap, don't stack
        if current and current[-1] == "(":
            return current              # no dangling operator after "("
        return current + key

    if key == "(":
        # An implicit multiply reads better than "5(" doing nothing.
        if current and (current[-1].isdigit() or current[-1] == ")"):
            return current + "*("
        return "(" if current == "0" else current + "("

    if key == ")":
        if current.count("(") <= current.count(")"):
            return current              # nothing open to close
        if current and current[-1] in "+-*/(":
            return current              # would close an empty group
        return current + ")"

    return current


def main(page: ft.Page):
    page.title = "Calculator"
    page.window.width = 340
    page.window.height = 500
    page.window.resizable = False
    page.padding = 15
    page.theme_mode = ft.ThemeMode.DARK

    result = ft.Text(value="0", size=40, color=ft.Colors.WHITE, selectable=True)

    def button_click(e):
        result.value = apply_key(result.value, e.control.data)
        page.update()

    def btn(label, data=None, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            color=ft.Colors.WHITE):
        return ft.ElevatedButton(
            text=label,
            data=data or label,
            on_click=button_click,
            expand=True,
            style=ft.ButtonStyle(
                color=color,
                bgcolor=bgcolor,
                shape=ft.RoundedRectangleBorder(radius=10),
                padding=15,
            ),
        )

    op_bg = ft.Colors.ORANGE_800
    action_bg = ft.Colors.BLUE_GREY_700

    page.add(
        ft.Container(
            content=result,
            alignment=ft.alignment.center_right,
            padding=ft.padding.only(right=15, top=20, bottom=20),
            bgcolor=ft.Colors.BLACK,
            border_radius=10,
        ),
        ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
        ft.Row([btn("C", bgcolor=action_bg), btn("("), btn(")"),
                btn("/", bgcolor=op_bg)]),
        ft.Row([btn("7"), btn("8"), btn("9"), btn("*", bgcolor=op_bg)]),
        ft.Row([btn("4"), btn("5"), btn("6"), btn("-", bgcolor=op_bg)]),
        ft.Row([btn("1"), btn("2"), btn("3"), btn("+", bgcolor=op_bg)]),
        ft.Row([btn("0"), btn("."), btn("⌫", data="back", bgcolor=action_bg),
                btn("=", bgcolor=op_bg)]),
    )


if __name__ == "__main__":
    ft.run(main)

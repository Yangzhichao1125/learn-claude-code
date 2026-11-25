"""Minimal greeting example used by the s05 todo_write lesson."""


def greet(name: str) -> None:
    """Print a greeting for ``name``.

    Args:
        name: The name of the person or entity to greet.
    """
    message: str = "Hello, " + name
    print(message)


if __name__ == "__main__":
    greet("Claude")

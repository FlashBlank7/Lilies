from calculator import add, multiply


def test_add() -> None:
    assert add(7, 5) == 12


def test_multiply() -> None:
    assert multiply(7, 5) == 35


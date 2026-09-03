"""Обвязка тестов.

Плагинов не требуется: хук ниже сам крутит корутины через asyncio.run, так что
pytest-asyncio не нужен. Один тест — один event loop — одна база в памяти,
поэтому тесты не тащат состояние друг в друга.

    python -m pytest tests -q

Прогон Монте-Карло по объёму из плана (1 млн раундов на игру, минуты вместо
секунд):

    $env:MC_ROUNDS = '1000000'; python -m pytest tests -q
"""

import asyncio
import inspect
import sys
from pathlib import Path

# Корень проекта в sys.path: тесты импортируют db, config и games напрямую,
# независимо от того, из какой папки запущен pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


def pytest_configure(config) -> None:
    config.addinivalue_line('markers', 'slow: длинный прогон Монте-Карло')


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Запускает async-тесты. Замена pytest-asyncio на семь строк."""
    fn = pyfuncitem.obj
    if not inspect.iscoroutinefunction(fn):
        return None
    kwargs = {name: pyfuncitem.funcargs[name]
              for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(fn(**kwargs))
    return True

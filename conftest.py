# -*- coding: utf-8 -*-
"""Отключаем автозагрузку внешних pytest-плагинов (в песочнице libtmux-плагин ломает запуск).
На боевой машине хост-скрипты это не затрагивает."""
import os
os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

"""Точка входа для cron в панели хостинга: панель запускает её своим Python
(любой версии), а она передаёт управление Python из venv бота."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "venv", "bin", "python")
os.chdir(HERE)
# вывод бота — в cron.log рядом (панель вывод задания не сохраняет)
log = os.open(os.path.join(HERE, "cron.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(log, 1)
os.dup2(log, 2)
os.execv(PY, [PY, os.path.join(HERE, "run_cron.py")])

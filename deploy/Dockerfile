# Зависимостей нет — только стандартная библиотека Python, поэтому образ
# тонкий и собирается за секунды.
FROM python:3.12-slim

WORKDIR /app
COPY *.py ./
COPY aliases.json ./

# Конфиг не копируем: он монтируется томом, чтобы секреты не попали в образ.
ENV PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["python3", "bot.py"]

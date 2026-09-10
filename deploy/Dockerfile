# Зависимостей нет — только стандартная библиотека Python.
FROM python:3.12-slim

# Учётный день и время отчёта считаются по Киеву. На UTC отчёт «в 22:00»
# уходил бы в час ночи, а «сегодня» менялось бы посреди вечерней смены.
ENV TZ=Europe/Kyiv PYTHONUNBUFFERED=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app
COPY *.py aliases.json ./

EXPOSE 8787
CMD ["python3", "bot.py"]

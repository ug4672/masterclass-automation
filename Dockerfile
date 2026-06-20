FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY hub.html .
COPY event.html .
COPY months.html .
COPY compare.html .
COPY styles.css .
COPY ["Masterclass Automation.html", "."]
EXPOSE 8080
CMD ["python", "server.py"]

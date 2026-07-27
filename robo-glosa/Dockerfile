# A versao da imagem PRECISA bater com a versao do pacote playwright no
# requirements.txt. Se divergirem, o Playwright nao acha os navegadores.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_recurso_glosa.py .

EXPOSE 8000

# Sem --host 0.0.0.0 aqui nao funciona: dentro do container, 127.0.0.1 so
# aceitaria conexoes do proprio container. A protecao vem de NAO expor a
# porta para fora do Docker (ver docker-compose.yaml).
CMD ["uvicorn", "bot_recurso_glosa:app", "--host", "0.0.0.0", "--port", "8000"]

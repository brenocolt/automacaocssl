# Imagem Python simples + o proprio Playwright instalando o navegador dele.
# Assim a versao do navegador SEMPRE bate com a do pacote, sem depender de
# adivinhar a tag correta de uma imagem pre-construida.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps instala as bibliotecas de sistema que o Chromium precisa.
# Baixa so o chromium (nao firefox/webkit) para a imagem nao ficar gigante.
RUN playwright install --with-deps chromium

COPY bot_recurso_glosa.py .

EXPOSE 8000

# 0.0.0.0 aqui e necessario: dentro do container, 127.0.0.1 recusaria a
# conexao vinda do n8n. A protecao vem de nao publicar a porta nem definir
# dominio no Coolify (ver docker-compose.yaml).
CMD ["uvicorn", "bot_recurso_glosa:app", "--host", "0.0.0.0", "--port", "8000"]

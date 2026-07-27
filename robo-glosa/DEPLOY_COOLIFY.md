# Implantação no Coolify

Como o n8n já roda no Coolify, o robô entra como um segundo recurso no **mesmo projeto**.
Assim os dois conversam pela rede interna do Docker, sem passar pela internet e sem expor
o robô publicamente.

## 1. Montar o repositório

O Coolify faz deploy a partir de um repositório Git. Crie um (pode ser privado) com:

```
robo-glosa/
├── bot_recurso_glosa.py
├── requirements.txt
├── Dockerfile
└── docker-compose.yaml
```

## 2. Criar o recurso no Coolify

1. Entre no **mesmo projeto** onde está o n8n (isso importa para a rede).
2. **+ New Resource** → **Docker Compose** (ou *Application* → Build Pack: Dockerfile).
3. Aponte para o repositório Git.
4. Em **Environment Variables**, cadastre:

```
SULAMERICA_PORTAL_URL=https://saude.sulamericaseguros.com.br/prestador/login/
SULAMERICA_RGE_URL=https://saude.sulamericaseguros.com.br/prestador/servicos-medicos/recurso-de-glosa-eletronico/rge/
SULAMERICA_CODIGO=083193290001
SULAMERICA_USER=usuario_do_robo
SULAMERICA_PASS=senha_do_robo
```

Marque as duas últimas como **secret/build-time hidden**, para não ficarem visíveis nos logs.

5. **NÃO configure domínio** para este recurso, e não adicione `ports` no compose.
   Sem domínio e sem porta publicada, o robô fica inacessível pela internet — só o n8n
   o alcança, por dentro da rede Docker. É a proteção mais importante aqui, já que quem
   chamar esse endpoint consegue usar as credenciais do cliente no portal.

6. Se existir a opção **Connect to Predefined Network**, ative — garante que o container
   entre na rede compartilhada do Coolify junto com o n8n.

7. **Deploy**.

## 3. Apontar o n8n para o robô

Dentro do Docker, `localhost` é o próprio container — não adianta usar `127.0.0.1`.
Os containers se encontram pelo **nome do serviço**.

No n8n, crie a variável de ambiente:

```
BOT_URL=http://robo-glosa:8000
```

(`robo-glosa` é o nome do serviço no `docker-compose.yaml`. Se você renomear lá, mude aqui.)

O nó "Chamar Robô (Playwright)" do workflow já usa `{{ $env.BOT_URL }}`, então não precisa
editar o nó.

### Se não conectar

Abra o terminal do container do n8n pelo próprio Coolify e teste:

```bash
wget -qO- http://robo-glosa:8000/health
```

Esperado: `{"status":"ok"}`.

Se der "bad address", os containers estão em redes diferentes. Confira se ambos estão no
mesmo projeto e se o "Connect to Predefined Network" está ativo nos dois. Como alternativa,
descubra o nome real do container (`docker ps`) e use-o no lugar de `robo-glosa` — o
Coolify acrescenta um sufixo único ao nome, mas mantém o nome do serviço como alias.

## 4. A planilha

Com o n8n em container, ler arquivo do disco exige montar volume e manter disciplina de
pasta. Recomendo trocar o nó "Início Manual" por um **Form Trigger**:

- o n8n gera um link com campo de upload;
- o cliente sobe a planilha e recebe a versão preenchida de volta;
- ninguém precisa saber caminho de pasta, e não há risco de processar arquivo velho.

Se preferir pasta fixa mesmo assim, monte um volume persistente no recurso do n8n
(ex: `/data/planilhas`) e use esse caminho — o de dentro do container, não o da VPS.

## 5. Recursos da máquina

O Chromium é pesado. Com n8n + robô na mesma VPS, conte com **4 GB de RAM**. Com 2 GB
funciona, mas lotes grandes podem ser interrompidos pelo sistema por falta de memória.

Ver consumo:

```bash
docker stats
```

## 6. Atualizações do robô

Quando a SulAmérica mudar o portal e algum seletor quebrar:

1. corrija o `bot_recurso_glosa.py`;
2. faça commit e push;
3. clique em **Redeploy** no Coolify.

Vale deixar o **Automatic Deployment** ligado para subir sozinho a cada push.

## 7. Checklist antes de entregar

- [ ] `wget -qO- http://robo-glosa:8000/health` responde de dentro do n8n
- [ ] Robô SEM domínio configurado e SEM portas publicadas
- [ ] Senha do portal cadastrada como secret no Coolify, não em arquivo no repositório
- [ ] `.env` e credenciais fora do repositório Git (adicione `.env` ao `.gitignore`)
- [ ] Usuário do portal é exclusivo do robô, não o login pessoal de um colaborador
- [ ] Teste completo com o lote 6300000499, conferindo contra a planilha
- [ ] Cliente sabe o que significa a marcação `REVISAR MANUALMENTE`
- [ ] Combinado quem dá manutenção quando o portal mudar

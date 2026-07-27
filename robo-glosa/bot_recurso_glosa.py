"""
bot_recurso_glosa.py

Robô de extração do Recurso de Glosa Eletrônica (Portal SulAmérica, Recursar Mat/Med).
Expõe um endpoint HTTP (FastAPI) que o n8n chama uma vez por Lote.

SELETORES: os seletores de login, navegação, filtros e do modal de recursos foram
extraídos do HTML real via `playwright codegen` (não são palpites). Os pontos ainda
marcados com "CONFIRMAR" são os que dependem da estrutura interna das tabelas de
resultado — use o script inspecionar.py para capturar o HTML delas.

O sistema é JSF/PrimeFaces: os IDs contêm ":" e por isso usamos seletores de
atributo [id="..."] em vez de #id (que exigiria escapar os dois-pontos).
"""

import os
import re
import logging
from datetime import date
from calendar import monthrange
from typing import Optional, List, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_recurso_glosa")

PORTAL_URL = os.environ.get(
    "SULAMERICA_PORTAL_URL",
    "https://saude.sulamericaseguros.com.br/prestador/login/",
)
RGE_URL = os.environ.get(
    "SULAMERICA_RGE_URL",
    "https://saude.sulamericaseguros.com.br/prestador/servicos-medicos/recurso-de-glosa-eletronico/rge/",
)
PORTAL_CODIGO = os.environ["SULAMERICA_CODIGO"]
PORTAL_USER = os.environ["SULAMERICA_USER"]
PORTAL_PASS = os.environ["SULAMERICA_PASS"]
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
DELAY_MS = int(os.environ.get("DELAY_ENTRE_ACOES_MS", "400"))

# IDs reais confirmados via playwright codegen
ID_LOTE = '[id="formFiltros:loteConta:loteConta"]'
ID_DATA_INI = "formFiltros:dataPagamentoConta"
ID_DATA_FIM = "formFiltros:dataPagamentoContaFim"
ID_TABELA_GUIAS = "formGrid:formGrid:gridTable"
ID_TABELA_RECURSOS = "formGrid:formRecursos:gridTableRecursos"
ID_TABELA_ITENS = "formRecursar:itensRecursoTable"
ID_DATA_REALIZACAO = "visualizarDadosGuia:dataRealizacao"

app = FastAPI(title="Bot Recurso de Glosa - SulAmérica")


class ProcessarLoteRequest(BaseModel):
    lote: str
    data_inicio_pagto: str   # dd/mm/aaaa
    data_fim_pagto: str      # dd/mm/aaaa


class ItemExtraido(BaseModel):
    guia: str = ""
    protocolo: str = ""
    data_recurso: Optional[str] = None
    data_complemento: Optional[str] = None
    valor_unit: Optional[str] = None
    data_uso: Optional[str] = None
    descricao_item: Optional[str] = None
    codigo_item: Optional[str] = None
    cod_glosa: Optional[str] = None
    qtde: Optional[int] = None
    justificativa: Optional[str] = None
    revisao_manual: bool = False
    erro: Optional[str] = None


# ------------------------------------------------------------- utilitários ----

def calcular_range_mes(data_iso: str) -> tuple:
    """'2025-03-17' -> ('01/03/2025', '31/03/2025')"""
    d = date.fromisoformat(data_iso)
    ultimo = monthrange(d.year, d.month)[1]
    return (
        date(d.year, d.month, 1).strftime("%d/%m/%Y"),
        date(d.year, d.month, ultimo).strftime("%d/%m/%Y"),
    )


def pausa_humana(page: Page):
    page.wait_for_timeout(DELAY_MS)


def aguardar_pagina_pronta(page: Page, timeout: int = 20000):
    """Evita 'networkidle', que nunca acontece em sites com analytics rodando."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PWTimeout:
        pass
    page.wait_for_timeout(800)


def aguardar_ajax(page: Page, timeout: int = 25000):
    """O sistema usa jQuery blockUI: enquanto processa, cobre a tela com um
    overlay. Esperar ele sumir evita ler a pagina no meio do carregamento."""
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('.blockUI.blockOverlay').length === 0",
            timeout=timeout,
        )
    except Exception:
        pass
    page.wait_for_timeout(400)


def capturar_screenshot_erro(page: Page, nome: str) -> str:
    import time
    caminho = f"debug_{nome}_{int(time.time())}.png"
    try:
        page.screenshot(path=caminho, full_page=True)
    except Exception:
        pass
    return caminho


def fechar_banner_cookies(page: Page):
    try:
        page.get_by_role("button", name=re.compile("^Continuar$", re.I)).click(timeout=4000)
    except Exception:
        pass


def _localizar_input_data(page: Page, base_id: str):
    """Devolve (locator, seletor) do input de texto do calendário PrimeFaces."""
    for seletor in [f'[id="{base_id}_input"]', f'[id="{base_id}"] input']:
        try:
            loc = page.locator(seletor).first
            if loc.count() > 0:
                return loc, seletor
        except Exception:
            continue
    return None, None


def preencher_data_primefaces(page: Page, base_id: str, valor: str):
    """Os campos de data são calendários PrimeFaces. Tentamos escrever direto no
    input (bem mais estável que navegar pelo widget) e SEMPRE conferimos se o
    valor realmente ficou lá — falha silenciosa aqui faz a busca voltar vazia
    sem ninguém perceber."""
    campo, seletor = _localizar_input_data(page, base_id)
    if campo is None:
        raise RuntimeError(f"Não encontrei o campo de data '{base_id}'")

    def conferir() -> bool:
        try:
            return campo.input_value(timeout=2000).strip() == valor
        except Exception:
            return False

    # 1) fill direto
    try:
        campo.fill(valor, timeout=4000)
        page.keyboard.press("Escape")
        if conferir():
            return
    except Exception:
        pass

    # 2) clicar, limpar e digitar como um humano
    try:
        campo.click(timeout=4000)
        page.keyboard.press("Meta+A")
        page.keyboard.press("Delete")
        page.keyboard.type(valor, delay=60)
        page.keyboard.press("Escape")
        if conferir():
            return
    except Exception:
        pass

    # 3) via JavaScript (funciona mesmo se o input for readonly)
    try:
        page.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.removeAttribute('readonly');
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            [seletor, valor],
        )
        if conferir():
            return
    except Exception:
        pass

    atual = ""
    try:
        atual = campo.input_value()
    except Exception:
        pass
    raise RuntimeError(
        f"Não consegui preencher a data em '{base_id}'. "
        f"Esperado '{valor}', campo ficou com '{atual}'"
    )


# -------------------------------------------------------- passos do fluxo ----

def fazer_login(page: Page):
    page.goto(PORTAL_URL)
    aguardar_pagina_pronta(page)
    fechar_banner_cookies(page)
    try:
        page.locator("#code").fill(PORTAL_CODIGO, timeout=8000)
        page.locator("#user").fill(PORTAL_USER, timeout=8000)
        page.locator("#senha").fill(PORTAL_PASS, timeout=8000)
        page.get_by_role("button", name=re.compile("^Entrar$", re.I)).click()
        aguardar_pagina_pronta(page)
    except Exception as e:
        caminho = capturar_screenshot_erro(page, "login")
        raise RuntimeError(f"Falha no login ({e}). Print salvo em: {caminho}")


def abrir_rge(page: Page) -> Page:
    """O sistema RGE abre numa janela separada (popup). Devolve essa nova página."""
    try:
        with page.expect_popup(timeout=20000) as info:
            page.goto(RGE_URL)
        rge = info.value
    except Exception:
        # Em alguns casos pode abrir na mesma aba em vez de popup
        rge = page
    aguardar_pagina_pronta(rge)
    return rge


def ir_para_matmed(rge: Page):
    # Sem regex aqui: a barra em "Mat/Med" quebra o parser de seletores do
    # Playwright (ele usa / para delimitar a própria expressão regular).
    rge.get_by_role("link", name="Recursar Mat/Med").click()
    # Aparece um modal de aviso com botão OK
    try:
        rge.get_by_role("button", name=re.compile("^OK$", re.I)).click(timeout=6000)
    except Exception:
        pass
    aguardar_pagina_pronta(rge)


def pesquisar_lote(rge: Page, lote: str, data_inicio: str, data_fim: str):
    try:
        rge.locator(ID_LOTE).fill(lote, timeout=8000)

        # CRÍTICO: com esse checkbox marcado, guias já recursadas NÃO aparecem.
        # Como buscamos o histórico de recursos já enviados, ele precisa ficar
        # desmarcado. Fazemos isso ANTES das datas porque esse clique dispara
        # um recarregamento parcial do PrimeFaces que pode resetar os campos.
        try:
            chk = rge.get_by_role("checkbox", name=re.compile("Exibir somente as guias", re.I))
            if chk.is_checked():
                chk.uncheck()
                pausa_humana(rge)
        except Exception:
            log.warning("Não consegui verificar o checkbox 'Exibir somente as guias'")

        preencher_data_primefaces(rge, ID_DATA_INI, data_inicio)
        preencher_data_primefaces(rge, ID_DATA_FIM, data_fim)
        log.info("Filtros preenchidos: lote=%s, %s a %s", lote, data_inicio, data_fim)

        pausa_humana(rge)
        rge.get_by_role("button", name=re.compile("^Pesquisar$", re.I)).click()
        aguardar_pagina_pronta(rge)
    except Exception as e:
        caminho = capturar_screenshot_erro(rge, "pesquisa")
        raise RuntimeError(f"Falha na pesquisa do lote {lote} ({e}). Print: {caminho}")


def listar_guias(rge: Page) -> List[Dict]:
    """Tabela de resultados. As celulas tem classes nomeadas (grid-lote,
    grid-guia, grid-paciente...), confirmadas no HTML real - bem mais
    estaveis do que indices de coluna."""
    linhas = rge.locator(f'[id="{ID_TABELA_GUIAS}_data"] tr[data-ri]')
    guias = []
    for i in range(linhas.count()):
        linha = linhas.nth(i)
        if "ui-datatable-empty-message" in (linha.get_attribute("class") or ""):
            continue

        def celula(classe: str) -> str:
            try:
                return linha.locator(f"td.{classe}").first.inner_text().strip()
            except Exception:
                return ""

        guias.append({
            "row_index": i,
            "lote": celula("grid-lote"),
            "guia": celula("grid-guia"),
            "paciente": celula("grid-paciente"),
            "data_pagamento": celula("grid-pgto"),
            "data_atendimento": celula("grid-atend"),
        })
    return guias


def abrir_detalhes_guia(rge: Page, row_index: int):
    linhas = rge.locator(f'[id="{ID_TABELA_GUIAS}_data"] tr[data-ri]')
    linhas.nth(row_index).click()
    pausa_humana(rge)
    rge.get_by_role("button", name=re.compile("Detalhes da Guia", re.I)).click()
    aguardar_ajax(rge)
    rge.locator(f'[id="{ID_TABELA_RECURSOS}_data"]').wait_for(timeout=20000)
    pausa_humana(rge)


def listar_protocolos(rge: Page) -> List[Dict]:
    """Modal 'Detalhes da Guia'. Cada protocolo aparece em duas linhas:
    envio (link 'linkVisualizarRecurso', Data Retorno vazia) e retorno
    (link 'linkVisualizarRetorno', Data Retorno preenchida). Agrupamos as
    duas pelo numero do protocolo.
    Colunas: 0 Tipo | 1 Status | 2 Protocolo | 3 Data Recurso |
             4 Data Retorno | 5 Recursado | 6 Qtd Itens | 7 Inf | 8 Lib |
             9 Acatado | 10 Prazo"""
    linhas = rge.locator(f'[id="{ID_TABELA_RECURSOS}_data"] tr[data-ri]')
    protocolos: Dict[str, Dict] = {}
    ordem: List[str] = []

    for i in range(linhas.count()):
        linha = linhas.nth(i)
        celulas = linha.locator("td")
        if celulas.count() < 8:
            continue
        protocolo = celulas.nth(2).inner_text().strip()
        if not protocolo:
            continue
        data_recurso = celulas.nth(3).inner_text().strip()
        data_retorno = celulas.nth(4).inner_text().strip()
        inf = celulas.nth(7).inner_text().strip()

        # So a linha de ENVIO abre a tela com a tabela de itens
        eh_envio = linha.locator(f'[id$=":{i}:linkVisualizarRecurso"]').count() > 0

        if protocolo not in protocolos:
            protocolos[protocolo] = {"protocolo": protocolo, "row_index": None}
            ordem.append(protocolo)
        entry = protocolos[protocolo]
        if eh_envio and entry["row_index"] is None:
            entry["row_index"] = i
        if data_recurso and not entry.get("data_recurso"):
            entry["data_recurso"] = data_recurso
        if data_retorno:
            entry["data_complemento"] = data_retorno
        if inf and not entry.get("valor_unit"):
            entry["valor_unit"] = inf

    return [protocolos[p] for p in ordem]


def abrir_visualizar_protocolo(rge: Page, row_index: int) -> Dict:
    """Clica no link de envio da linha e le a tela 'Visualizar protocolo'."""
    rge.locator(f'[id="{ID_TABELA_RECURSOS}:{row_index}:linkVisualizarRecurso"]').click()
    aguardar_ajax(rge)
    aguardar_pagina_pronta(rge)

    data_uso = ""
    try:
        data_uso = rge.locator(f'[id="{ID_DATA_REALIZACAO}"]').inner_text(timeout=10000).strip()
    except Exception:
        log.warning("Nao consegui ler 'Data da realizacao'")

    itens = []
    try:
        rge.locator(f'[id="{ID_TABELA_ITENS}"]').wait_for(timeout=20000)
        linhas = rge.locator(f'[id="{ID_TABELA_ITENS}_data"] tr[data-ri]')
        for i in range(linhas.count()):
            celulas = linhas.nth(i).locator("td")
            textos = [celulas.nth(c).inner_text().strip() for c in range(celulas.count())]
            if not textos:
                continue
            cod_servico = next((t for t in textos if " - " in t), "")
            codigo, _, descricao = cod_servico.partition(" - ")
            numeros = [t for t in textos if t.replace(".", "").isdigit()]
            itens.append({
                "codigo": codigo.strip().lstrip("0") or "0",
                "descricao": descricao.strip(),
                "cod_glosa": numeros[1] if len(numeros) > 1 else "",
                "qtde": int(numeros[-1]) if numeros and numeros[-1].isdigit() else 0,
                "justificativa": "",
                "_colunas_cruas": textos,
            })
    except Exception as e:
        log.warning("Nao consegui ler a tabela de itens: %s", e)

    return {"data_uso": data_uso, "itens": itens}


def consolidar_itens(itens: List[Dict]) -> Dict:
    """Um protocolo vira UMA linha na planilha (ver spec, seção 5.2)."""
    if not itens:
        return {"revisao_manual": True, "erro": "Nenhum item encontrado no protocolo"}
    tipos = {(it["codigo"], it["descricao"]) for it in itens}
    if len(tipos) > 1:
        return {"revisao_manual": True, "erro": "Protocolo com itens de códigos diferentes"}
    base = itens[0]
    return {
        "descricao_item": base["descricao"],
        "codigo_item": base["codigo"],
        "cod_glosa": base["cod_glosa"],
        "qtde": sum(it["qtde"] for it in itens),
        "justificativa": base["justificativa"],
        "revisao_manual": False,
    }


# --------------------------------------------------------------- endpoint ----

@app.post("/processar-lote", response_model=List[ItemExtraido])
def processar_lote(req: ProcessarLoteRequest):
    """IMPORTANTE: abrir um protocolo e um submit de formulario JSF, que sai
    da tela de resultados e descarta a busca. Por isso refazemos a pesquisa
    antes de cada protocolo, em vez de tentar 'voltar' na navegacao."""
    resultados: List[ItemExtraido] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_context().new_page()
        try:
            try:
                fazer_login(page)
                rge = abrir_rge(page)
                ir_para_matmed(rge)
            except Exception as e:
                raise HTTPException(status_code=502, detail=str(e))

            def preparar(guia_index: Optional[int] = None):
                """Refaz a busca; opcionalmente ja abre o modal de uma guia."""
                pesquisar_lote(rge, req.lote, req.data_inicio_pagto, req.data_fim_pagto)
                if guia_index is not None:
                    abrir_detalhes_guia(rge, guia_index)

            preparar()
            guias = listar_guias(rge)
            log.info("Lote %s: %d guia(s)", req.lote, len(guias))
            if not guias:
                return resultados

            for guia in guias:
                rotulo = f"{guia['guia']} - {guia['paciente']}"
                try:
                    preparar(guia["row_index"])
                    protocolos = listar_protocolos(rge)
                    log.info("Guia %s: %d protocolo(s)", guia["guia"], len(protocolos))

                    for prot in protocolos:
                        if prot.get("row_index") is None:
                            resultados.append(ItemExtraido(
                                guia=guia["guia"], protocolo=prot["protocolo"],
                                data_recurso=prot.get("data_recurso"),
                                data_complemento=prot.get("data_complemento"),
                                valor_unit=prot.get("valor_unit"),
                                revisao_manual=True,
                                erro="Protocolo sem linha de envio (so retorno)",
                            ))
                            continue
                        try:
                            preparar(guia["row_index"])
                            detalhe = abrir_visualizar_protocolo(rge, prot["row_index"])
                            consolidado = consolidar_itens(detalhe["itens"])
                            resultados.append(ItemExtraido(
                                guia=guia["guia"],
                                protocolo=prot["protocolo"],
                                data_recurso=prot.get("data_recurso"),
                                data_complemento=prot.get("data_complemento"),
                                valor_unit=prot.get("valor_unit"),
                                data_uso=detalhe["data_uso"],
                                **consolidado,
                            ))
                        except Exception as e:
                            log.exception("Falha no protocolo %s", prot.get("protocolo"))
                            capturar_screenshot_erro(rge, f"protocolo_{prot.get('protocolo')}")
                            resultados.append(ItemExtraido(
                                guia=guia["guia"], protocolo=prot.get("protocolo", ""),
                                erro=str(e), revisao_manual=True,
                            ))
                except Exception as e:
                    log.exception("Falha na guia %s", rotulo)
                    resultados.append(ItemExtraido(
                        guia=guia.get("guia", ""), erro=str(e), revisao_manual=True,
                    ))
        finally:
            browser.close()

    return resultados


@app.get("/health")
def health():
    return {"status": "ok"}

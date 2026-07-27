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
ID_CHECKBOX_DISPONIVEIS = "formFiltros:somenteGuiasDisponiveis"
ID_BTN_PESQUISAR = "formFiltros:btnPesquisar"
ID_BTN_DETALHES = "formGrid:formButtons:modalDialogButton"
MENU_POR_ABA = {"matmed": "formLink:menuMatMed", "itens": "formLink:menuItens"}
ABAS = ("matmed", "itens")
ID_TABELA_RECURSOS = "formGrid:formRecursos:gridTableRecursos"
ID_TABELA_ITENS = "formRecursar:itensRecursoTable"
IDS_DATA_REALIZACAO = (
    "visualizarDadosGuia:dataRealizacao",     # aba Mat/Med
    "dadosGuiaContaGlosada:dataRealizacao",   # aba Itens
)

app = FastAPI(title="Bot Recurso de Glosa - SulAmérica")


class ProcessarLoteRequest(BaseModel):
    lote: str
    data_inicio_pagto: str   # dd/mm/aaaa
    data_fim_pagto: str      # dd/mm/aaaa


class ItemExtraido(BaseModel):
    aba: str = ""
    guia: str = ""
    protocolo: str = ""
    data_recurso: Optional[str] = None
    data_complemento: Optional[str] = None
    valor_unit: Optional[str] = None
    valor_total: Optional[str] = None
    valor_acatado: Optional[str] = None
    qtd_itens_protocolo: Optional[str] = None
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


def ir_para_aba(rge: Page, aba: str):
    """As duas abas (Mat/Med e Itens) usam exatamente os mesmos IDs de campo e
    de tabela - confirmado inspecionando o HTML das duas. So muda o caminho de
    entrada e o estado inicial do checkbox."""
    # Ao terminar uma aba, o modal "Detalhes da Guia" costuma ficar aberto e
    # o overlay dele bloqueia o clique no menu da outra aba.
    fechar_modal(rge)

    menu = MENU_POR_ABA[aba]
    rotulo = "Recursar Mat/Med" if aba == "matmed" else "Recursar Itens"
    tentativas = [
        ("menu do topo", lambda: rge.locator(f'[id="{menu}"]').click(timeout=10000)),
        ("link da tela inicial", lambda: rge.get_by_role("link", name=rotulo).click(timeout=8000)),
        ("menu via JavaScript", lambda: rge.evaluate(
            "(id) => { const el = document.getElementById(id); if (!el) throw new Error('menu nao encontrado'); el.click(); }",
            menu)),
    ]

    erros = []
    for descricao, acao in tentativas:
        try:
            acao()
            log.info("Entrei na aba %s pelo %s", aba, descricao)
            break
        except Exception as e:
            erros.append(f"{descricao}: {type(e).__name__}")
            continue
    else:
        caminho = capturar_screenshot_erro(rge, f"aba_{aba}")
        raise RuntimeError(
            f"Nao consegui abrir a aba {aba}. Tentativas: {'; '.join(erros)}. Print: {caminho}"
        )

    try:  # modal de aviso com botao OK
        rge.get_by_role("button", name=re.compile("^OK$", re.I)).click(timeout=6000)
    except Exception:
        pass
    aguardar_ajax(rge)
    aguardar_pagina_pronta(rge)


# Mantido para o inspecionar.py continuar funcionando
def ir_para_matmed(rge: Page):
    ir_para_aba(rge, "matmed")


def fechar_modal(rge: Page):
    """Fecha o modal 'Detalhes da Guia' se estiver aberto. Enquanto ele existe,
    o overlay bloqueia qualquer clique na tela de pesquisa por tras."""
    try:
        botao = rge.locator(".ui-dialog-titlebar-close:visible")
        if botao.count() > 0:
            botao.first.click(timeout=3000)
            rge.wait_for_timeout(500)
    except Exception:
        try:
            rge.keyboard.press("Escape")
            rge.wait_for_timeout(300)
        except Exception:
            pass


def garantir_tela_de_pesquisa(rge: Page, aba: str = "matmed"):
    """Abrir um protocolo tira o robo da tela de pesquisa (e um submit JSF).
    Antes de cada nova busca, garante que estamos de volta nela."""
    fechar_modal(rge)
    if rge.locator(ID_LOTE).count() > 0:
        return

    for seletor in [f'[id="{MENU_POR_ABA[aba]}"]', "#menu2"]:
        try:
            rge.locator(seletor).first.click(timeout=8000)
            aguardar_ajax(rge)
            aguardar_pagina_pronta(rge)
            try:  # pode reaparecer o aviso com botao OK
                rge.get_by_role("button", name=re.compile("^OK$", re.I)).click(timeout=4000)
                aguardar_ajax(rge)
            except Exception:
                pass
            if rge.locator(ID_LOTE).count() > 0:
                return
        except Exception:
            continue

    # Ultimo recurso: reentrar pela aba do zero
    try:
        ir_para_aba(rge, aba)
        if rge.locator(ID_LOTE).count() > 0:
            return
    except Exception:
        pass

    caminho = capturar_screenshot_erro(rge, "voltar_pesquisa")
    raise RuntimeError(
        f"Nao consegui voltar para a tela de pesquisa da aba {aba}. Print: {caminho}"
    )


def preencher_lote(rge: Page, lote: str):
    """Limpa e preenche o campo Lote, CONFERINDO o valor depois. Sem limpar
    antes, o valor novo pode grudar em restos do anterior (ex: pedir
    6300000641 e o portal receber 630000064163), e a busca volta vazia sem
    deixar claro o motivo."""
    campo = rge.locator(ID_LOTE)
    campo.wait_for(timeout=15000)

    def conferir() -> bool:
        try:
            return campo.input_value(timeout=3000).strip() == lote
        except Exception:
            return False

    # 1) limpar e preencher
    try:
        campo.fill("", timeout=5000)
        rge.wait_for_timeout(150)
        campo.fill(lote, timeout=5000)
        if conferir():
            return
    except Exception:
        pass

    # 2) selecionar tudo e digitar por cima
    try:
        campo.click(timeout=5000)
        rge.keyboard.press("ControlOrMeta+A")
        rge.keyboard.press("Delete")
        rge.keyboard.type(lote, delay=40)
        if conferir():
            return
    except Exception:
        pass

    # 3) via JavaScript, disparando os eventos que o JSF escuta
    try:
        rge.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.value = '';
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            [ID_LOTE, lote],
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
    raise RuntimeError(f"Campo Lote ficou com '{atual}' em vez de '{lote}'")


def desmarcar_somente_disponiveis(rge: Page):
    """Com esse filtro ligado, guias ja recursadas somem do resultado - e e
    justamente o historico que queremos. No Mat/Med vem desmarcado; na aba
    Itens vem MARCADO por padrao."""
    try:
        campo = rge.locator(f'[id="{ID_CHECKBOX_DISPONIVEIS}"]')
        if campo.count() and campo.is_checked():
            campo.uncheck(timeout=6000)
            pausa_humana(rge)
    except Exception as e:
        log.warning("Nao consegui desmarcar 'somente guias disponiveis': %s", e)


def pesquisar_lote(rge: Page, lote: str, data_inicio: str, data_fim: str,
                   aba: str = "matmed"):
    try:
        garantir_tela_de_pesquisa(rge, aba)
        preencher_lote(rge, lote)

        # CRÍTICO: com esse checkbox marcado, guias já recursadas NÃO aparecem.
        # Como buscamos o histórico de recursos já enviados, ele precisa ficar
        # desmarcado. Fazemos isso ANTES das datas porque esse clique dispara
        # um recarregamento parcial do PrimeFaces que pode resetar os campos.
        desmarcar_somente_disponiveis(rge)

        preencher_data_primefaces(rge, ID_DATA_INI, data_inicio)
        preencher_data_primefaces(rge, ID_DATA_FIM, data_fim)
        log.info("Filtros preenchidos: lote=%s, %s a %s", lote, data_inicio, data_fim)

        pausa_humana(rge)
        rge.locator(f'[id="{ID_BTN_PESQUISAR}"]').click()
        aguardar_ajax(rge)
        aguardar_pagina_pronta(rge)
        esperar_tabela_guias(rge)

        # Se voltou vazio, tenta de novo uma vez (pode ser ajax perdido) e,
        # se insistir, informa o estado real dos filtros em vez de so falhar.
        if contar_linhas_guias(rge) == 0:
            log.warning("Busca do lote %s voltou vazia; tentando novamente", lote)
            rge.wait_for_timeout(1500)
            rge.locator(f'[id="{ID_BTN_PESQUISAR}"]').click()
            aguardar_ajax(rge)
            aguardar_pagina_pronta(rge)
            esperar_tabela_guias(rge)

        if contar_linhas_guias(rge) == 0:
            estado = ler_estado_filtros(rge)
            caminho = capturar_screenshot_erro(rge, "busca_vazia")
            raise RuntimeError(
                f"Busca do lote {lote} na aba {aba} nao retornou linhas. "
                f"Filtros como o portal recebeu: {estado}. Print: {caminho}"
            )
    except Exception as e:
        caminho = capturar_screenshot_erro(rge, "pesquisa")
        raise RuntimeError(f"Falha na pesquisa do lote {lote} ({e}). Print: {caminho}")


def ler_estado_filtros(rge: Page) -> Dict:
    """Le de volta o que REALMENTE ficou nos filtros. Serve para diagnosticar
    buscas que voltam vazias: mostra se o valor digitado foi aceito pelo
    PrimeFaces ou se algo reverteu para o padrao."""
    estado = {}
    try:
        estado["lote"] = rge.locator(ID_LOTE).input_value(timeout=3000)
    except Exception:
        estado["lote"] = "<nao lido>"
    for rotulo, base in (("data_inicio", ID_DATA_INI), ("data_fim", ID_DATA_FIM)):
        try:
            campo, _ = _localizar_input_data(rge, base)
            estado[rotulo] = campo.input_value(timeout=3000) if campo else "<nao encontrado>"
        except Exception:
            estado[rotulo] = "<nao lido>"
    try:
        chk = rge.get_by_role("checkbox", name=re.compile("Exibir somente as guias", re.I))
        estado["checkbox_somente_disponiveis"] = chk.is_checked(timeout=3000)
    except Exception:
        estado["checkbox_somente_disponiveis"] = "<nao lido>"
    try:
        vazio = rge.locator(f'[id="{ID_TABELA_GUIAS}_data"] tr.ui-datatable-empty-message')
        estado["mensagem_tabela"] = vazio.first.inner_text(timeout=2000) if vazio.count() else ""
    except Exception:
        estado["mensagem_tabela"] = "<nao lido>"
    return estado


def contar_linhas_guias(rge: Page) -> int:
    try:
        return rge.locator(f'[id="{ID_TABELA_GUIAS}_data"] tr[data-ri]').count()
    except Exception:
        return 0


def esperar_tabela_guias(rge: Page, timeout: int = 30000):
    """Espera a tabela de resultados terminar de renderizar - seja com linhas,
    seja com a mensagem de 'nenhum item'. Sem isso, ler a tabela cedo demais
    faz parecer que a busca nao retornou nada."""
    seletor = (
        f'[id="{ID_TABELA_GUIAS}_data"] tr[data-ri], '
        f'[id="{ID_TABELA_GUIAS}_data"] tr.ui-datatable-empty-message'
    )
    try:
        rge.locator(seletor).first.wait_for(timeout=timeout)
    except Exception:
        log.warning("Tabela de guias nao renderizou dentro do tempo esperado")


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
    esperar_tabela_guias(rge)
    linhas = rge.locator(f'[id="{ID_TABELA_GUIAS}_data"] tr[data-ri]')
    linha = linhas.nth(row_index)
    linha.wait_for(timeout=30000)
    linha.click()
    pausa_humana(rge)
    rge.locator(f'[id="{ID_BTN_DETALHES}"]').click()
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
        recursado = celulas.nth(5).inner_text().strip()   # -> "Valor total"
        qtd_itens = celulas.nth(6).inner_text().strip()
        inf = celulas.nth(7).inner_text().strip()         # -> "Valor Unit"
        acatado = celulas.nth(9).inner_text().strip()     # -> "Vl Recuperado"

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
        if recursado and not entry.get("valor_total"):
            entry["valor_total"] = recursado
        if qtd_itens and not entry.get("qtd_itens_protocolo"):
            entry["qtd_itens_protocolo"] = qtd_itens
        if acatado and not entry.get("valor_acatado"):
            entry["valor_acatado"] = acatado

    return [protocolos[p] for p in ordem]


def abrir_visualizar_protocolo(rge: Page, row_index: int) -> Dict:
    """Clica no link de envio da linha e le a tela 'Visualizar protocolo'."""
    rge.locator(f'[id="{ID_TABELA_RECURSOS}:{row_index}:linkVisualizarRecurso"]').click()
    aguardar_ajax(rge)
    aguardar_pagina_pronta(rge)

    data_uso = ""
    for id_data in IDS_DATA_REALIZACAO:
        try:
            alvo = rge.locator(f'[id="{id_data}"]')
            if alvo.count():
                data_uso = alvo.first.inner_text(timeout=5000).strip()
                if data_uso:
                    break
        except Exception:
            continue
    if not data_uso:
        log.warning("Nao consegui ler 'Data da realizacao'")

    itens = []
    try:
        try:
            rge.locator(f'[id="{ID_TABELA_ITENS}"]').wait_for(timeout=8000)
        except Exception:
            rge.wait_for_timeout(1500)   # aba Itens usa outro ID

        brutos = rge.evaluate(
            """(idTabela) => {
                let tbody = document.getElementById(idTabela + '_data');
                if (!tbody) {
                    // aba diferente pode usar outro id: procura pela coluna
                    for (const d of document.querySelectorAll('div.ui-datatable')) {
                        if (/C.d\\.?\\s*Servi/i.test(d.innerText || '')) {
                            tbody = d.querySelector('tbody[id$="_data"]');
                            if (tbody) break;
                        }
                    }
                }
                if (!tbody) return null;

                const linhas = Array.from(tbody.querySelectorAll('tr'));
                const saida = [];
                for (let i = 0; i < linhas.length; i++) {
                    const tr = linhas[i];
                    if (!tr.hasAttribute('data-ri')) continue;
                    const celulas = Array.from(tr.querySelectorAll('td'))
                        .map(td => (td.innerText || '').trim());

                    // A justificativa pode estar numa celula desta linha (Mat/Med)
                    // ou numa linha irma escondida logo abaixo (Itens).
                    let justificativa = '';
                    const prox = linhas[i + 1];
                    if (prox && !prox.hasAttribute('data-ri') &&
                        /justificativa/i.test(prox.innerText || '')) {
                        justificativa = prox.innerText || '';
                    }
                    saida.push({ celulas, justificativa });
                }
                return saida;
            }""",
            ID_TABELA_ITENS,
        )

        if brutos is None:
            caminho = capturar_screenshot_erro(rge, "sem_tabela_itens")
            raise RuntimeError(f"Tabela de itens nao encontrada. Print: {caminho}")

        for bruto in brutos:
            itens.append(_extrair_item(bruto["celulas"], bruto.get("justificativa", "")))
    except Exception as e:
        log.warning("Nao consegui ler a tabela de itens: %s", e)

    return {"data_uso": data_uso, "itens": itens}


def _localizar_tabela_itens(rge: Page):
    """A tabela de itens tem ID diferente em cada aba. Em vez de fixar o do
    Mat/Med, tenta o conhecido e, se nao achar, procura qualquer DataTable que
    tenha a coluna 'Cod. Servico'."""
    alvo = rge.locator(f'[id="{ID_TABELA_ITENS}_data"]')
    if alvo.count():
        return alvo.first

    for texto in ("Cód. Serviço", "Cod. Servico", "Cód. Glosa"):
        candidatos = rge.locator(
            f'div.ui-datatable:has-text("{texto}") tbody[id$="_data"]'
        )
        if candidatos.count():
            tid = candidatos.first.get_attribute("id")
            log.info("Tabela de itens encontrada por conteudo: %s", tid)
            return candidatos.first

    return None


def _limpar_justificativa(texto: str) -> str:
    """O texto vem com o rotulo junto: em varias linhas no Mat/Med
    ('Justificativa\\n...\\nItem recursado - ...') e numa linha so na aba
    Itens ('Justificativa Solicito reanalise...')."""
    if not texto:
        return ""
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    linhas = [l for l in linhas if l.lower() != "justificativa"]
    resultado = linhas[-1] if linhas else ""
    return re.sub(r"^justificativa[:\s]*", "", resultado, flags=re.I).strip()


def _extrair_item(textos: List[str], justificativa_externa: str = "") -> Dict:
    """Identifica cada campo pelo CONTEUDO, nao pela posicao. As duas abas tem
    layouts diferentes - a de Itens traz uma coluna vazia a mais entre
    "Cod. Servico" e "Cod. Glosa" - e ler por deslocamento fixo pegava a
    coluna errada em todos os campos.

    Mat/Med: [.., '22', '0000273772 - TIRA...', '1705', 'R$ 3,95', '1', ..]
    Itens  : ['', '15', '60023120 - TAXA...', '', '1714', 'R$ 22,10', '1', '']
    """
    def so_digitos(t: str) -> bool:
        return bool(t) and t.replace(".", "").isdigit()

    # 1) a celula do servico e a unica com " - " e sem quebra de linha
    idx_servico = next(
        (i for i, t in enumerate(textos) if " - " in t and "\n" not in t), None
    )
    if idx_servico is None:
        return {
            "seq": "", "codigo": "", "descricao": "", "cod_glosa": "",
            "valor_glosado": "", "qtde": 0,
            "justificativa": _limpar_justificativa(justificativa_externa),
            "_colunas_cruas": textos,
        }

    cod_servico = textos[idx_servico]
    codigo, _, descricao = cod_servico.partition(" - ")

    depois = textos[idx_servico + 1:]

    # 2) o valor glosado e a celula com "R$"
    idx_valor = next((i for i, t in enumerate(depois) if "R$" in t), None)
    valor_glosado = depois[idx_valor] if idx_valor is not None else ""

    # 3) cod. glosa: celula numerica ANTES do valor
    faixa_glosa = depois[:idx_valor] if idx_valor is not None else depois
    cod_glosa = next((t for t in faixa_glosa if so_digitos(t)), "")

    # 4) qtde: primeira celula numerica DEPOIS do valor
    faixa_qtde = depois[idx_valor + 1:] if idx_valor is not None else []
    qtde_txt = next((t for t in faixa_qtde if so_digitos(t)), "")

    # 5) seq: ultima celula numerica ANTES do servico
    seq = ""
    for t in textos[:idx_servico]:
        if so_digitos(t):
            seq = t

    # 6) justificativa: linha irma escondida (Itens) ou celula da propria linha (Mat/Med)
    justificativa = justificativa_externa
    if not justificativa:
        justificativa = next((t for t in textos if "justificativa" in t.lower()), "")

    return {
        "seq": seq,
        "codigo": codigo.strip().lstrip("0") or "0",
        "descricao": descricao.strip(),
        "cod_glosa": cod_glosa,
        "valor_glosado": valor_glosado,
        "qtde": int(qtde_txt) if qtde_txt.isdigit() else 0,
        "justificativa": _limpar_justificativa(justificativa),
        "_colunas_cruas": textos,
    }


def consolidar_itens(itens: List[Dict]) -> Dict:
    """Regra confirmada com o cliente: um protocolo vira UMA linha na planilha,
    usando os dados do PRIMEIRO item. Isso e valido porque a justificativa e a
    mesma para todos os itens do protocolo (a soma dos itens forma o valor da
    glosa, que fica no campo 'Valor total').

    Se as justificativas divergirem, a premissa cai por terra e a linha e
    sinalizada para revisao em vez de gravar um dado enganoso."""
    if not itens:
        return {"revisao_manual": True, "erro": "Nenhum item encontrado no protocolo"}

    justificativas = {it["justificativa"] for it in itens if it["justificativa"]}
    base = itens[0]
    resultado = {
        "descricao_item": base["descricao"],
        "codigo_item": base["codigo"],
        "cod_glosa": base["cod_glosa"],
        "qtde": base["qtde"],          # do 1o item, nao a soma (confirmado)
        "justificativa": base["justificativa"],
        "revisao_manual": False,
    }
    if len(justificativas) > 1:
        resultado["revisao_manual"] = True
        lista = " | ".join(sorted(justificativas))
        resultado["erro"] = (
            f"Protocolo com {len(justificativas)} justificativas diferentes "
            f"entre os {len(itens)} itens: {lista}"
        )
    return resultado


# --------------------------------------------------------------- endpoint ----

@app.post("/processar-lote", response_model=List[ItemExtraido])
def processar_lote(req: ProcessarLoteRequest):
    """Percorre AS DUAS abas (Mat/Med e Itens). Confirmado com o cliente: a
    planilha reune protocolos das duas, e buscar so em Mat/Med deixava cerca
    de dois tercos dos protocolos de fora.

    Abrir um protocolo e um submit JSF que descarta a busca; por isso a
    pesquisa e refeita antes de cada protocolo."""
    resultados: List[ItemExtraido] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_context().new_page()
        try:
            try:
                fazer_login(page)
                rge = abrir_rge(page)
            except Exception as e:
                raise HTTPException(status_code=502, detail=str(e))

            for aba in ABAS:
                try:
                    ir_para_aba(rge, aba)
                except Exception as e:
                    log.exception("Falha ao entrar na aba %s", aba)
                    resultados.append(ItemExtraido(
                        aba=aba, erro=f"Nao consegui abrir a aba {aba}: {e}",
                        revisao_manual=True,
                    ))
                    continue

                def preparar(guia_index: Optional[int] = None, _aba=aba):
                    pesquisar_lote(rge, req.lote, req.data_inicio_pagto,
                                   req.data_fim_pagto, _aba)
                    if guia_index is not None:
                        abrir_detalhes_guia(rge, guia_index)

                try:
                    preparar()
                    guias = listar_guias(rge)
                except Exception as e:
                    # Lote sem nada nesta aba e situacao normal, nao erro.
                    log.info("Aba %s, lote %s: sem resultados (%s)", aba, req.lote, e)
                    continue

                log.info("Aba %s, lote %s: %d guia(s)", aba, req.lote, len(guias))

                for guia in guias:
                    try:
                        preparar(guia["row_index"])
                        protocolos = listar_protocolos(rge)
                        log.info("  guia %s: %d protocolo(s)", guia["guia"], len(protocolos))

                        for prot in protocolos:
                            if prot.get("row_index") is None:
                                resultados.append(ItemExtraido(
                                    aba=aba, guia=guia["guia"], protocolo=prot["protocolo"],
                                    data_recurso=prot.get("data_recurso"),
                                    data_complemento=prot.get("data_complemento"),
                                    valor_unit=prot.get("valor_unit"),
                                    valor_total=prot.get("valor_total"),
                                    revisao_manual=True,
                                    erro="Protocolo sem linha de envio (so retorno)",
                                ))
                                continue
                            try:
                                preparar(guia["row_index"])
                                detalhe = abrir_visualizar_protocolo(rge, prot["row_index"])
                                consolidado = consolidar_itens(detalhe["itens"])
                                resultados.append(ItemExtraido(
                                    aba=aba,
                                    guia=guia["guia"],
                                    protocolo=prot["protocolo"],
                                    data_recurso=prot.get("data_recurso"),
                                    data_complemento=prot.get("data_complemento"),
                                    valor_unit=prot.get("valor_unit"),
                                    valor_total=prot.get("valor_total"),
                                    valor_acatado=prot.get("valor_acatado"),
                                    qtd_itens_protocolo=prot.get("qtd_itens_protocolo"),
                                    data_uso=detalhe["data_uso"],
                                    **consolidado,
                                ))
                            except Exception as e:
                                log.exception("Falha no protocolo %s", prot.get("protocolo"))
                                capturar_screenshot_erro(rge, f"prot_{prot.get('protocolo')}")
                                resultados.append(ItemExtraido(
                                    aba=aba, guia=guia["guia"],
                                    protocolo=prot.get("protocolo", ""),
                                    erro=str(e), revisao_manual=True,
                                ))
                    except Exception as e:
                        log.exception("Falha na guia %s (aba %s)", guia.get("guia"), aba)
                        resultados.append(ItemExtraido(
                            aba=aba, guia=guia.get("guia", ""),
                            erro=str(e), revisao_manual=True,
                        ))
        finally:
            browser.close()

    log.info("Lote %s finalizado: %d registro(s)", req.lote, len(resultados))
    return resultados


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------- preenchimento da planilha ----
#
# Recriar a planilha a partir de JSON (como o no "JSON -> Excel" do n8n faz)
# destroi formatacao de data, formato monetario, larguras de coluna, estilos e
# abas extras. Aqui abrimos o arquivo ORIGINAL e escrevemos apenas nas celulas
# necessarias, preservando todo o resto.

import json
from io import BytesIO
from copy import copy
from datetime import datetime

import openpyxl
from fastapi import File, Form, UploadFile
from fastapi.responses import StreamingResponse

LINHA_CABECALHO = 2
PRIMEIRA_LINHA_DADOS = 3
COL_LOTE_PLANILHA = "PROTOCOLO DE ENTREGA"
COL_PROTOCOLO = "PROTOCOLO RECURSO"
COL_STATUS = "Status Robô"


def _moeda(v):
    if v in (None, ""):
        return None
    limpo = re.sub(r"[^\d,.-]", "", str(v)).replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def _data(v):
    if not v:
        return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", str(v).strip())
    return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _num(v):
    if v in (None, ""):
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return v


def _mapa_colunas(ws) -> Dict[str, int]:
    return {
        ws.cell(row=LINHA_CABECALHO, column=c).value: c
        for c in range(1, ws.max_column + 1)
        if ws.cell(row=LINHA_CABECALHO, column=c).value
    }


def _garantir_coluna_status(ws, colunas: Dict[str, int]) -> int:
    if COL_STATUS in colunas:
        return colunas[COL_STATUS]
    nova = ws.max_column + 1
    celula = ws.cell(row=LINHA_CABECALHO, column=nova)
    celula.value = COL_STATUS
    modelo = ws.cell(row=LINHA_CABECALHO, column=1)
    celula.font = copy(modelo.font)
    celula.fill = copy(modelo.fill)
    celula.border = copy(modelo.border)
    celula.alignment = copy(modelo.alignment)
    colunas[COL_STATUS] = nova
    return nova


COLUNAS_DATA = {"DATA DO RECURSO", "Data uso", "Data Recurso 1", "Data Retorno 1",
                "Data Recurso 2", "Data Retorno 2"}
COLUNAS_MOEDA = {"Valor Unit", "Valor total", "Valor Recurso", "Vl Recuperado",
                 "Valor Pendente"}


def _formatos_por_coluna(ws, colunas: Dict[str, int]) -> Dict[str, str]:
    """Descobre o formato que a planilha ja usa em cada coluna. Celulas vazias
    nao tem formato, entao escrever nelas sem definir number_format faria a
    data aparecer como numero de serie (o 45742 que voce viu)."""
    formatos = {}
    for nome, col in colunas.items():
        # Datas sempre em dd/mm/aaaa: a planilha original mistura dd/mm/yy,
        # mm-dd-yy e texto na mesma coluna, e herdar isso deixaria o resultado
        # dependente do idioma do Excel de quem abrir.
        if nome in COLUNAS_DATA:
            formatos[nome] = "dd/mm/yyyy"
            continue
        encontrado = None
        for r in range(PRIMEIRA_LINHA_DADOS, min(ws.max_row, PRIMEIRA_LINHA_DADOS + 400) + 1):
            celula = ws.cell(row=r, column=col)
            if celula.value is not None and celula.number_format != "General":
                encontrado = celula.number_format
                break
        if not encontrado:
            if nome in COLUNAS_DATA:
                encontrado = "dd/mm/yy"
            elif nome in COLUNAS_MOEDA:
                encontrado = "#,##0.00"
        if encontrado:
            formatos[nome] = encontrado
    return formatos


def _copiar_estilo_da_linha(ws, origem: int, destino: int, ultima_coluna: int):
    for c in range(1, ultima_coluna + 1):
        de = ws.cell(row=origem, column=c)
        para = ws.cell(row=destino, column=c)
        para.font = copy(de.font)
        para.fill = copy(de.fill)
        para.border = copy(de.border)
        para.alignment = copy(de.alignment)
        para.number_format = de.number_format


@app.post("/preencher-planilha")
async def preencher_planilha(arquivo: UploadFile = File(...), linhas: str = Form(...)):
    """Recebe a planilha original + os protocolos extraidos e devolve o mesmo
    arquivo preenchido, com formatacao intacta."""
    resultados = json.loads(linhas)
    if isinstance(resultados, dict):
        resultados = [resultados]

    wb = openpyxl.load_workbook(BytesIO(await arquivo.read()))
    ws = wb["Modificado"] if "Modificado" in wb.sheetnames else wb[wb.sheetnames[0]]

    colunas = _mapa_colunas(ws)
    formatos = _formatos_por_coluna(ws, colunas)
    col_status = _garantir_coluna_status(ws, colunas)
    ultima_coluna = ws.max_column

    # Onde termina o grupo de cada lote e em que linha esta cada protocolo
    fim_do_lote: Dict[str, int] = {}
    linha_do_protocolo: Dict[str, int] = {}
    lote_atual = None
    for r in range(PRIMEIRA_LINHA_DADOS, ws.max_row + 1):
        lote = ws.cell(row=r, column=colunas[COL_LOTE_PLANILHA]).value
        if lote not in (None, ""):
            lote_atual = str(lote).strip()
        if lote_atual:
            fim_do_lote[lote_atual] = r
        prot = ws.cell(row=r, column=colunas[COL_PROTOCOLO]).value
        if prot not in (None, ""):
            linha_do_protocolo[str(prot).strip()] = r

    atualizadas = criadas = ignoradas = 0

    for res in resultados:
        protocolo = str(res.get("protocolo") or "").strip()
        if not protocolo:
            ignoradas += 1
            continue

        destino = linha_do_protocolo.get(protocolo)
        if destino is None:
            # Protocolo novo: entra logo apos a ultima linha do lote, para
            # ficar junto dos irmaos em vez de solto no fim da planilha.
            lote_res = str(res.get("lote") or "").strip()
            ancora = fim_do_lote.get(lote_res, ws.max_row)
            destino = ancora + 1
            ws.insert_rows(destino)
            _copiar_estilo_da_linha(ws, ancora, destino, ultima_coluna)
            # Os indices abaixo do ponto de insercao andam uma linha
            for k, v in list(linha_do_protocolo.items()):
                if v >= destino:
                    linha_do_protocolo[k] = v + 1
            for k, v in list(fim_do_lote.items()):
                if v >= destino:
                    fim_do_lote[k] = v + 1
            if lote_res in fim_do_lote:
                fim_do_lote[lote_res] = destino
            linha_do_protocolo[protocolo] = destino
            criadas += 1
        else:
            atualizadas += 1

        valor_recurso = _moeda(res.get("valor_total"))
        valor_recuperado = _moeda(res.get("valor_acatado"))
        pendente = (
            round(valor_recurso - valor_recuperado, 2)
            if valor_recurso is not None and valor_recuperado is not None
            else None
        )

        valores = {
            "Guia": res.get("guia"),
            COL_PROTOCOLO: _num(protocolo),
            "DATA DO RECURSO": _data(res.get("data_recurso")),
            "Data uso": _data(res.get("data_uso")),
            "Descrição Item": res.get("descricao_item"),
            "Codigo Item": _num(res.get("codigo_item")),
            "Valor Unit": _moeda(res.get("valor_unit")),
            "Valor total": valor_recurso,
            "Qtde": res.get("qtde"),
            "Cod glosa": _num(res.get("cod_glosa")),
            "Justificativa recurso": res.get("justificativa"),
            "Valor Recurso": valor_recurso,
            "Vl Recuperado": valor_recuperado,
            "Valor Pendente": pendente,
            "Data Recurso 1": _data(res.get("data_recurso")),
            "Data Retorno 1": _data(res.get("data_complemento")),
        }

        for nome, valor in valores.items():
            col = colunas.get(nome)
            if col and valor is not None:
                celula = ws.cell(row=destino, column=col)
                celula.value = valor
                if nome in formatos:
                    celula.number_format = formatos[nome]

        motivo = res.get("erro")
        ws.cell(row=destino, column=col_status).value = (
            f"REVISAR: {motivo}" if res.get("revisao_manual") and motivo
            else ("REVISAR MANUALMENTE" if res.get("revisao_manual") else "OK")
        )

    # Totais da linha 1 acompanhando o novo tamanho da planilha
    for letra in ("E", "G", "H", "J"):
        celula = ws[f"{letra}1"]
        if celula.value is not None:
            celula.value = f"=SUBTOTAL(9,{letra}{PRIMEIRA_LINHA_DADOS}:{letra}{ws.max_row})"

    log.info("Planilha preenchida: %d atualizada(s), %d criada(s), %d ignorada(s)",
             atualizadas, criadas, ignoradas)

    saida = BytesIO()
    wb.save(saida)
    saida.seek(0)
    return StreamingResponse(
        saida,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="planilha_preenchida.xlsx"'},
    )

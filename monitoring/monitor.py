import asyncio
import aiohttp
from collections import defaultdict, deque
from datetime import datetime
from bot.utils import escape_markdown_v2, send_telegram_message
from config import ROULETTES, HISTORICO_MAX

PADRAO_12 = [2, 4, 5, 6, 12, 16, 21, 24, 27, 28, 34, 35]
HISTORICO_COMPLETO_SIZE = 500
TENDENCIA_UPDATE_INTERVAL = 10
MINIMO_OCORRENCIAS = 5
MINIMO_RODADAS_ANALISE = 50

API_URL = "https://casino.dougurasu-bets.online:9000/playtech/results.json"
LINK_MESA_BASE = "https://geralbet.bet.br/live-casino/game/3763038"

estado_mesas = defaultdict(
    lambda: {
        "status": "idle",
        "entrada": None,
        "gale": 0,
        "monitorando": False,
        "greens": 0,
        "greens_g1": 0,
        "greens_g2": 0,
        "loss": 0,
        "total": 0,
        "consec_greens": 0,
        "ultimo_resultado_validado": None,
        "data_atual": datetime.now().date(),
        "historico": deque(maxlen=HISTORICO_COMPLETO_SIZE),
        "sinais_enviados": 0,
        "aguardando_confirmacao": False,
        "tendencias": {},
        "top_tendencias": [],
        "contador_rodadas": 0,
        "ultima_atualizacao_tendencias": None,
        "entrada_ativa": False,
        "numero_entrada": None,
        "modo_real": False,
        "entradas_reais_restantes": 0,
        "validacoes_silenciosas_consec_greens": 0,
        "aguardando_loss_para_resetar": False,
        "alerta_enviado": False,
        "ultimo_numero_processado": None,
        "aguardando_setima_entrada": False,
        "setima_entrada_numero": None,
    }
)


def pertence_ao_padrao(numero):
    return numero in PADRAO_12


def analisar_tendencias(historico):
    historico = list(historico)
    tendencias = {n: {"chamou_12": 0, "total": 0} for n in range(37)}

    for idx in range(3, len(historico)):
        numero_atual = historico[idx]
        anteriores = historico[idx - 3 : idx][::-1]

        for anterior in anteriores:
            if pertence_ao_padrao(anterior):
                tendencias[numero_atual]["chamou_12"] += 1
                break

        tendencias[numero_atual]["total"] += 1

    for numero in tendencias:
        total = tendencias[numero]["total"]
        chamou_12 = tendencias[numero]["chamou_12"]
        porcentagem = round((chamou_12 / total * 100), 2) if total > 0 else 0
        tendencias[numero]["porcentagem"] = porcentagem

    return tendencias


def get_top_tendencias(tendencias, n=10):
    filtrado = {
        k: v
        for k, v in tendencias.items()
        if v["total"] >= MINIMO_OCORRENCIAS and v["porcentagem"] >= 80
    }
    return sorted(filtrado.items(), key=lambda x: -x[1]["porcentagem"])[:n]


async def notificar_entrada(roulette_id, numero, tendencias):
    stats = tendencias[numero]
    message = f"🔥 ENTRADA Padrão 12 - {numero} ({stats['chamou_12']}/{stats['total']})\n"
    await send_telegram_message(message, LINK_MESA_BASE)


async def fetch_results_http(session, mesa_nome):
    async with session.get(API_URL) as resp:
        data = await resp.json()
        resultados = data.get(mesa_nome, {}).get("results", [])
        return [int(r["number"]) for r in resultados if r.get("number", "").isdigit()]


async def monitor_roulette(roulette_id):
    print(f"[INICIANDO] Monitorando mesa: {roulette_id}")
    mesa = estado_mesas[roulette_id]

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                hoje = datetime.now().date()
                if mesa["data_atual"] != hoje:
                    mesa.update(
                        {
                            "greens": 0,
                            "greens_g1": 0,
                            "greens_g2": 0,
                            "loss": 0,
                            "total": 0,
                            "consec_greens": 0,
                            "data_atual": hoje,
                            "sinais_enviados": 0,
                            "contador_rodadas": 0,
                            "validacoes_silenciosas_consec_greens": 0,
                            "aguardando_loss_para_resetar": False,
                            "alerta_enviado": False,
                            "modo_real": False,
                            "entradas_reais_restantes": 0,
                            "aguardando_setima_entrada": False,
                            "setima_entrada_numero": None,
                        }
                    )

                resultados = await fetch_results_http(session, roulette_id)
                if not resultados:
                    await asyncio.sleep(2)
                    continue

                mesa["historico"] = deque(
                    resultados[:HISTORICO_COMPLETO_SIZE], maxlen=HISTORICO_COMPLETO_SIZE
                )
                historico_size = len(mesa["historico"])
                mesa["contador_rodadas"] += 1

                if historico_size >= MINIMO_RODADAS_ANALISE:
                    nova_tendencia = analisar_tendencias(mesa["historico"])
                    novo_top = get_top_tendencias(nova_tendencia)
                    novo_top_numeros = [num for num, _ in novo_top]

                    mesa["tendencias"] = nova_tendencia
                    mesa["top_tendencias"] = novo_top_numeros

                    numero_atual = mesa["historico"][0]

                    if numero_atual == mesa["ultimo_numero_processado"]:
                        await asyncio.sleep(2)
                        continue

                    mesa["ultimo_numero_processado"] = numero_atual

                    if not mesa["entrada_ativa"] and numero_atual in novo_top_numeros:
                        mesa["entrada_ativa"] = True
                        mesa["numero_entrada"] = numero_atual
                        mesa["gale"] = 0

                        if mesa["aguardando_setima_entrada"]:
                            mesa["setima_entrada_numero"] = numero_atual
                            print(
                                f"[SETIMA ENTRADA DETECTADA] Número {numero_atual} - Aguardando resultado..."
                            )

                    elif mesa["entrada_ativa"]:
                        if pertence_ao_padrao(numero_atual):
                            mesa["total"] += 1

                            if not mesa["modo_real"]:
                                if not mesa["aguardando_loss_para_resetar"]:
                                    mesa["validacoes_silenciosas_consec_greens"] += 1
                                    print(
                                        f"[GREEN SILENCIOSO #{mesa['validacoes_silenciosas_consec_greens']}] {numero_atual} | Mesa: {roulette_id}"
                                    )

                                    if (
                                        mesa["validacoes_silenciosas_consec_greens"]
                                        == 6
                                        and not mesa["alerta_enviado"]
                                        and not mesa["aguardando_setima_entrada"]
                                    ):
                                        message = (
                                            f"⚠️ POSSÍVEL ENTRADA Padrão 12 ⚠️\n\n"
                                        )
                                        await send_telegram_message(
                                            message, LINK_MESA_BASE
                                        )
                                        mesa["alerta_enviado"] = True
                                        mesa["aguardando_setima_entrada"] = True

                                    elif (
                                        mesa["aguardando_setima_entrada"]
                                        and mesa["setima_entrada_numero"]
                                        == mesa["numero_entrada"]
                                    ):
                                        mesa[
                                            "validacoes_silenciosas_consec_greens"
                                        ] += 1
                                        print(
                                            f"[SETIMA ENTRADA - GREEN!] 7 greens consecutivos validados! Liberando entradas reais..."
                                        )

                                        mesa["modo_real"] = True
                                        mesa["entradas_reais_restantes"] = 3
                                        mesa["alerta_enviado"] = False
                                        mesa["aguardando_setima_entrada"] = False
                                        mesa["setima_entrada_numero"] = None

                                        await send_telegram_message(
                                            "🚨 ENTRADAS LIBERADAS! 🚨\n\n"
                                            "✅ Padrão 12 validado com 7 greens consecutivos"
                                        )

                                else:
                                    print(
                                        f"[GREEN SILENCIOSO CONTINUA] {numero_atual} | Aguardando LOSS para resetar validação."
                                    )
                            else:
                                mesa["greens"] += 1
                                if mesa["gale"] == 1:
                                    mesa["greens_g1"] += 1
                                elif mesa["gale"] == 2:
                                    mesa["greens_g2"] += 1

                                await send_telegram_message(
                                    f"✅✅✅ GREEN!!! ✅✅✅\n\n({mesa['historico'][0]}|{mesa['historico'][1]}|{mesa['historico'][2]})\n\n"
                                    f"🎯 Entradas restantes: {mesa['entradas_reais_restantes'] - 1}"
                                )

                                mesa["entradas_reais_restantes"] -= 1
                                if mesa["entradas_reais_restantes"] <= 0:
                                    mesa["modo_real"] = False
                                    mesa["aguardando_loss_para_resetar"] = True
                                    print(
                                        "[CICLO COMPLETO] 3 entradas reais finalizadas. Aguardando LOSS para resetar."
                                    )

                            mesa["entrada_ativa"] = False
                            mesa["numero_entrada"] = None
                            mesa["gale"] = 0

                        elif mesa["gale"] == 0:
                            mesa["gale"] = 1
                            if mesa["modo_real"]:
                                await send_telegram_message(
                                    f"🔁 Primeiro GALE ({numero_atual})"
                                )
                        elif mesa["gale"] == 1:
                            mesa["gale"] = 2
                            if mesa["modo_real"]:
                                await send_telegram_message(
                                    f"🔁 Segundo e último GALE ({numero_atual})"
                                )
                        else:
                            if mesa["modo_real"]:
                                mesa["loss"] += 1
                                mesa["total"] += 1
                                await send_telegram_message(
                                    f"❌❌❌ LOSS!!! ❌❌❌\n\n({mesa['historico'][0]}|{mesa['historico'][1]}|{mesa['historico'][2]})\n\n"
                                    f"🎯 Entradas restantes: {mesa['entradas_reais_restantes'] - 1}"
                                )

                                mesa["entradas_reais_restantes"] -= 1
                                if mesa["entradas_reais_restantes"] <= 0:
                                    mesa["modo_real"] = False
                                    mesa["aguardando_loss_para_resetar"] = True
                                    print(
                                        "[CICLO COMPLETO] 3 entradas reais finalizadas. Aguardando LOSS para resetar."
                                    )
                            else:
                                if (
                                    mesa["aguardando_setima_entrada"]
                                    and mesa["setima_entrada_numero"]
                                    == mesa["numero_entrada"]
                                ):
                                    await send_telegram_message(
                                        "❌ ENTRADA CANCELADA ❌\n\n"
                                        "🎯 7ª entrada resultou em LOSS\n"
                                        "🔄 Reiniciando validação..."
                                    )
                                    mesa["aguardando_setima_entrada"] = False
                                    mesa["setima_entrada_numero"] = None

                                print(
                                    f"[LOSS SILENCIOSO] {numero_atual} - Resetando contador de greens consecutivos"
                                )
                                mesa["validacoes_silenciosas_consec_greens"] = 0
                                mesa["alerta_enviado"] = False
                                mesa["aguardando_loss_para_resetar"] = False

                            mesa["entrada_ativa"] = False
                            mesa["numero_entrada"] = None
                            mesa["gale"] = 0

                    if (
                        mesa["modo_real"]
                        and not mesa["entrada_ativa"]
                        and numero_atual in novo_top_numeros
                        and mesa["entradas_reais_restantes"] > 0
                    ):

                        await notificar_entrada(
                            roulette_id, numero_atual, nova_tendencia
                        )

                await asyncio.sleep(2)

            except Exception as e:
                print(f"[ERRO] {roulette_id}: {str(e)}")
                await asyncio.sleep(5)


async def start_all():
    tasks = [asyncio.create_task(monitor_roulette(mesa)) for mesa in ROULETTES]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(start_all())

#!/usr/bin/env python3
"""Editor automático de vídeos do Estúdio Nati.

Pega um vídeo bruto e um estilo (JSON) e entrega o vídeo editado:
  1. corta pausas e silêncios longos
  2. transcreve a fala (faster-whisper) com o tempo de cada palavra
  3. queima legendas no estilo do perfil (fonte, cor, destaque da palavra falada)
  4. ajusta formato (16:9 ou 9:16), volume da voz e música de fundo
  5. gera também o .srt e um relatório do que foi feito

Uso:
  python3 editar.py bruto.mp4 --estilo estilos/youtube.json --saida editado.mp4
  python3 editar.py bruto.mp4 --estilo estilos/reels.json --legendas texto.srt   # sem transcrever

Requisitos: ffmpeg e `pip install faster-whisper` (só para transcrever).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ESTILO_PADRAO = {
    "formato": "16:9",            # "16:9" ou "9:16"
    "altura": 1080,
    "cortar_pausas": True,
    "pausa_minima": 0.6,          # segundos de silêncio que viram corte
    "folga": 0.12,                # segundos mantidos antes e depois de cada fala
    "limite_silencio_db": -35,
    "legenda": {
        "ativa": True,
        "fonte": "Montserrat ExtraBold",
        "tamanho": 0.065,         # fração da altura do vídeo
        "cor": "#FFFFFF",
        "cor_destaque": "#FFD400",  # cor da palavra que está sendo falada
        "contorno": "#000000",
        "espessura_contorno": 0.006,
        "sombra": 0,
        "posicao": "baixo",       # "baixo", "meio" ou "cima"
        "margem": 0.12,           # fração da altura a partir da borda
        "maiusculas": True,
        "palavras_por_bloco": 3,
        "destacar_palavra": True,
    },
    "audio": {
        "normalizar": True,
        "musica": None,           # caminho de um arquivo de música
        "volume_musica": 0.12,
    },
    "intro": None,                # vídeo para colocar no início
    "outro": None,                # vídeo para colocar no fim
    "idioma": "pt",
    "modelo_whisper": "small",
}


def mesclar(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = mesclar(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def rodar(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f"Falhou: {' '.join(cmd[:4])}…")
    return r


def duracao(path: Path) -> float:
    r = rodar(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def tem_audio(path: Path) -> bool:
    r = rodar(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(r.stdout.strip())


# ---------- 1. Cortar pausas ----------

def trechos_com_fala(path: Path, est: dict) -> list[tuple[float, float]]:
    total = duracao(path)
    if not est["cortar_pausas"] or not tem_audio(path):
        return [(0.0, total)]
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"silencedetect=noise={est['limite_silencio_db']}dB:d={est['pausa_minima']}", "-f", "null", "-"],
        capture_output=True, text=True)
    inicios = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    fins = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    silencios = list(zip(inicios, fins + [total] * (len(inicios) - len(fins))))
    folga = est["folga"]
    trechos, cursor = [], 0.0
    for s, e in silencios:
        a, b = cursor, min(total, s + folga)
        if b - a > 0.15:
            trechos.append((a, b))
        cursor = max(cursor, e - folga)
    if total - cursor > 0.15:
        trechos.append((cursor, total))
    return trechos or [(0.0, total)]


def cortar(path: Path, trechos: list[tuple[float, float]], destino: Path, com_audio: bool) -> None:
    partes, rotulos = [], []
    for i, (a, b) in enumerate(trechos):
        partes.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}]")
        rotulos.append(f"[v{i}]")
        if com_audio:
            partes.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}]")
            rotulos.append(f"[a{i}]")
    n = len(trechos)
    saidas = "[v][a]" if com_audio else "[v]"
    partes.append(f"{''.join(rotulos)}concat=n={n}:v=1:a={1 if com_audio else 0}{saidas}")
    script = destino.with_suffix(".filtro.txt")
    script.write_text(";\n".join(partes))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-filter_complex_script", str(script), "-map", "[v]"]
    if com_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(destino)]
    rodar(cmd)


# ---------- 2. Transcrever ----------

@dataclass
class Palavra:
    inicio: float
    fim: float
    texto: str


def transcrever(path: Path, est: dict) -> list[Palavra]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("Instale o transcritor: pip install faster-whisper")
    modelo = WhisperModel(est["modelo_whisper"], device="auto", compute_type="int8")
    segs, _ = modelo.transcribe(str(path), language=est["idioma"], word_timestamps=True, vad_filter=True)
    out = []
    for s in segs:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                out.append(Palavra(w.start, w.end, t))
    return out


def ler_srt(path: Path) -> list[Palavra]:
    """Lê um .srt e distribui o tempo de cada bloco entre suas palavras."""
    def seg(t):
        h, m, s = t.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    out = []
    for bloco in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        linhas = bloco.strip().splitlines()
        tempo = next((l for l in linhas if "-->" in l), None)
        if not tempo:
            continue
        a, b = [seg(x.strip()) for x in tempo.split("-->")]
        texto = " ".join(linhas[linhas.index(tempo) + 1:]).split()
        passo = (b - a) / max(1, len(texto))
        out += [Palavra(a + i * passo, a + (i + 1) * passo, w) for i, w in enumerate(texto)]
    return out


# ---------- 3. Legendas ----------

def cor_ass(hexcor: str) -> str:
    h = hexcor.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def tempo_ass(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def tempo_srt(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60); ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{min(ms, 999):03d}"


def blocos(palavras: list[Palavra], n: int) -> list[list[Palavra]]:
    out, atual = [], []
    for w in palavras:
        atual.append(w)
        fecha = len(atual) >= n or re.search(r"[.!?…,]$", w.texto)
        if fecha:
            out.append(atual); atual = []
    if atual:
        out.append(atual)
    return out


def gerar_ass(palavras: list[Palavra], est: dict, largura: int, altura: int) -> str:
    L = est["legenda"]
    tam = round(altura * L["tamanho"])
    contorno = max(1, round(altura * L["espessura_contorno"]))
    align = {"baixo": 2, "meio": 5, "cima": 8}[L["posicao"]]
    margem = round(altura * L["margem"]) if L["posicao"] != "meio" else 0
    cab = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {largura}
PlayResY: {altura}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Nati,{L['fonte']},{tam},{cor_ass(L['cor'])},{cor_ass(L['cor_destaque'])},{cor_ass(L['contorno'])},&H80000000,-1,0,0,0,100,100,0,0,1,{contorno},{L['sombra']},{align},{round(largura*0.08)},{round(largura*0.08)},{margem},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    linhas = []
    destaque = cor_ass(L["cor_destaque"])
    def fmt(t):
        t = t.replace("{", "(").replace("}", ")")
        return t.upper() if L["maiusculas"] else t
    for bl in blocos(palavras, L["palavras_por_bloco"]):
        if L["destacar_palavra"]:
            for i, w in enumerate(bl):
                fim = bl[i + 1].inicio if i + 1 < len(bl) else w.fim
                txt = " ".join((f"{{\\c{destaque}}}{fmt(x.texto)}{{\\r}}" if j == i else fmt(x.texto)) for j, x in enumerate(bl))
                linhas.append(f"Dialogue: 0,{tempo_ass(w.inicio)},{tempo_ass(fim)},Nati,,0,0,0,,{txt}")
        else:
            txt = " ".join(fmt(x.texto) for x in bl)
            linhas.append(f"Dialogue: 0,{tempo_ass(bl[0].inicio)},{tempo_ass(bl[-1].fim)},Nati,,0,0,0,,{txt}")
    return cab + "\n".join(linhas) + "\n"


def gerar_srt(palavras: list[Palavra], n: int = 7) -> str:
    out = []
    for i, bl in enumerate(blocos(palavras, n), 1):
        out.append(f"{i}\n{tempo_srt(bl[0].inicio)} --> {tempo_srt(bl[-1].fim)}\n{' '.join(w.texto for w in bl)}\n")
    return "\n".join(out)


# ---------- 4. Montagem final ----------

def dimensoes(est: dict) -> tuple[int, int]:
    h = est["altura"]
    if est["formato"] == "9:16":
        return (round(h * 9 / 16) // 2 * 2, h) if h > 1080 else (1080, 1920)
    return (round(h * 16 / 9) // 2 * 2, h)


def finalizar(video: Path, ass: Path | None, est: dict, destino: Path, fontes: Path | None) -> None:
    W, H = dimensoes(est)
    filtros = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}", "setsar=1"]
    if ass:
        esc = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        sub = f"subtitles='{esc}'"
        if fontes:
            sub += f":fontsdir='{str(fontes)}'"
        filtros.append(sub)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    A = est["audio"]
    com_audio = tem_audio(video)
    musica = Path(A["musica"]) if A.get("musica") else None
    if musica:
        cmd += ["-stream_loop", "-1", "-i", str(musica)]
    fc = [f"[0:v]{','.join(filtros)}[v]"]
    if com_audio:
        voz = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[voz]" if A["normalizar"] else "[0:a]anull[voz]"
        fc.append(voz)
        if musica:
            fc.append(f"[1:a]volume={A['volume_musica']}[mus]")
            fc.append("[mus][voz]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
            fc.append("[voz][duck]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            fc.append("[voz]anull[a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]"]
    if com_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest", str(destino)]
    rodar(cmd)


def juntar(arquivos: list[Path], destino: Path, est: dict) -> None:
    W, H = dimensoes(est)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for a in arquivos:
        cmd += ["-i", str(a)]
    fc, rot = [], []
    for i, a in enumerate(arquivos):
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[v{i}]")
        if tem_audio(a):
            fc.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{duracao(a):.3f}[a{i}]")
        rot.append(f"[v{i}][a{i}]")
    fc.append(f"{''.join(rot)}concat=n={len(arquivos)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(destino)]
    rodar(cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description="Edita um vídeo bruto no estilo do perfil.")
    ap.add_argument("bruto", type=Path)
    ap.add_argument("--estilo", type=Path, help="JSON com o estilo do perfil")
    ap.add_argument("--saida", type=Path, help="arquivo final (padrão: <bruto>_editado.mp4)")
    ap.add_argument("--legendas", type=Path, help=".srt pronto; pula a transcrição")
    ap.add_argument("--fontes", type=Path, help="pasta com os arquivos de fonte (.ttf/.otf)")
    ap.add_argument("--sem-legenda", action="store_true")
    a = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("Instale o ffmpeg.")
    est = mesclar(ESTILO_PADRAO, json.loads(a.estilo.read_text(encoding="utf-8")) if a.estilo else {})
    if a.sem_legenda:
        est["legenda"]["ativa"] = False
    saida = a.saida or a.bruto.with_name(a.bruto.stem + "_editado.mp4")
    fontes = a.fontes or (Path(__file__).parent / "fontes")
    fontes = fontes if fontes.is_dir() else None

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        orig = duracao(a.bruto)
        print(f"Bruto: {a.bruto.name} ({orig:.1f}s)")

        trechos = trechos_com_fala(a.bruto, est)
        cortado = tmp / "cortado.mp4"
        cortar(a.bruto, trechos, cortado, tem_audio(a.bruto))
        dur = duracao(cortado)
        print(f"Pausas cortadas: {len(trechos)} trechos, {orig:.1f}s → {dur:.1f}s")

        ass = None
        palavras: list[Palavra] = []
        if est["legenda"]["ativa"]:
            if a.legendas:
                palavras = ler_srt(a.legendas)
            elif tem_audio(cortado):
                print("Transcrevendo a fala…")
                palavras = transcrever(cortado, est)
            if palavras:
                W, H = dimensoes(est)
                ass = tmp / "legenda.ass"
                ass.write_text(gerar_ass(palavras, est, W, H), encoding="utf-8")
                saida.with_suffix(".srt").write_text(gerar_srt(palavras), encoding="utf-8")
                print(f"Legendas: {len(palavras)} palavras")

        miolo = tmp / "miolo.mp4" if (est.get("intro") or est.get("outro")) else saida
        finalizar(cortado, ass, est, miolo, fontes)
        if miolo != saida:
            partes = [Path(p) for p in (est.get("intro"), ) if p] + [miolo] + [Path(p) for p in (est.get("outro"), ) if p]
            juntar(partes, saida, est)

        rel = {
            "bruto": str(a.bruto), "saida": str(saida), "duracao_bruto": round(orig, 1),
            "duracao_final": round(duracao(saida), 1), "trechos_mantidos": len(trechos),
            "palavras_legendadas": len(palavras), "formato": est["formato"],
        }
        saida.with_suffix(".json").write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Pronto: {saida} ({rel['duracao_final']}s)")


if __name__ == "__main__":
    main()

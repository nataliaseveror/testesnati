# Estúdio Nati

Sistema para gerenciar 4 canais com um assistente de IA:

| Perfil | Onde | Ligado à Zernio |
|---|---|---|
| Criação de conteúdo | Instagram @nati_severo | sim |
| Beleza | Instagram @natiseverobeauty | sim |
| Higienização de estofados | Instagram | ainda não |
| YouTube | canal do perfil de criação | ainda não |

## As três partes

1. **Painel** (`painel.html`, publicado em https://claude.ai/artifact/HCfqgwMr9ErAkjDWGvLSKe)
   Onde a Nati cadastra os perfis e o estilo de cada um, faz pedidos, acompanha a produção e aprova.
2. **Assistente** (Claude Code): lê os pedidos do painel, produz e devolve a entrega.
3. **Editor de vídeo** (`editor/editar.py`): corta pausas, legenda no estilo do perfil, ajusta formato e áudio.

## Banco de dados do painel

- `perfis/{criacao|beleza|estofados|youtube}`: nome, handle, nicho, publico, tom, pilares[], cores, fontes, zernioId, estilo{exemplos, legenda, ritmo, musica, formato, abertura}
- `pedidos/{id}`: perfilId, tipo (reel|carrossel|post|stories|youtube|roteiro|plano), titulo, ideia, tom, cta, dataPostagem, linkBruto, referencias, arquivos[{id,nome,tipo}], status, conversa[{de,texto,em}], entrega{resumo, roteiro, legenda, hashtags, links[{nome,url}]}
- `metricas/{perfilId}`: seguidores, alcance30d, engajamento, insights, topPosts[{titulo,tipo,alcance,link}], atualizadoEm

Status: `fila` → `produzindo` → `revisar` → (`ajustes` → `produzindo` …) → `aprovado` → `publicado`.

## Rotina do assistente ("processa os pedidos")

1. Ler `pedidos` com status `fila` ou `ajustes` (ArtifactData `query`). Ler o perfil de cada um.
2. Marcar `produzindo`.
3. Produzir conforme o tipo:
   - **Reel / YouTube**: pegar o arquivo `linkBruto` na pasta `Estúdio Nati/Brutos` do computador da Nati (conectado por Remote Control), gerar o estilo do perfil em JSON a partir de `perfil.estilo`, rodar `editor/editar.py` lá, salvar o resultado em `Estúdio Nati/Editados` e colocar o link em `entrega.links`. Escrever roteiro, título e legenda.
   - **Carrossel / Post**: texto de cada lâmina e arte no Canva com as cores e fontes do perfil; link do Canva em `entrega.links`.
   - **Roteiro**: gancho (3s), desenvolvimento, chamada para ação.
   - **Plano do mês**: calendário com dia, formato, tema e gancho, usando `metricas.insights`.
   - Em `ajustes`: ler o último item de `conversa` e refazer só o que foi pedido.
4. Escrever `entrega`, acrescentar uma mensagem do assistente em `conversa` e marcar `revisar`.
5. Pedidos `aprovado` com `dataPostagem`: agendar pela Zernio (`posts_create` com o `zernioId` do perfil) e marcar `publicado` quando sair.
6. "Atualiza o desempenho": ler analytics da Zernio de cada perfil e gravar em `metricas/{perfilId}`, com `insights` em linguagem simples (o que mais alcançou, melhor horário, formato que mais engaja).

## Editor de vídeo

```bash
pip install faster-whisper
python3 editor/editar.py bruto.mp4 --estilo editor/estilos/youtube.json --saida editado.mp4
```

Estilos prontos: `editor/estilos/youtube.json` (16:9) e `editor/estilos/reels.json` (9:16).
Fontes da marca: coloque os `.ttf` em `editor/fontes/`.
A transcrição baixa o modelo do Hugging Face na primeira vez, então precisa de acesso a `huggingface.co`.

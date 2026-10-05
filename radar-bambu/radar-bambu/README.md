# Radar Bambu

Monitora H2S e P1S (e variantes Combo/Laser) a cada 2h, calcula o **custo efetivo até Palmas**
(preço + frete; para Paraguai: USD × câmbio real + taxa do contato + imposto sobre excesso da cota),
guarda histórico e avisa no Telegram. Roda grátis no GitHub Actions; painel no GitHub Pages.

## Setup (≈15 min)
1. Crie um repositório **privado** no GitHub e suba estes arquivos.
2. Telegram: fale com @BotFather → `/newbot` → copie o token. Mande qualquer mensagem pro bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para pegar seu `chat.id`.
3. Repo → Settings → Secrets and variables → Actions → crie `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.
4. Mercado Livre (a busca pública pode exigir token): crie um app em developers.mercadolivre.com.br
   e cadastre `ML_CLIENT_ID` e `ML_CLIENT_SECRET` como secrets.
5. Actions → radar → **Run workflow** para a primeira leitura.
6. Painel: Settings → Pages → Branch `main`, pasta `/docs`. (Pages em repo privado exige plano pago;
   sem isso, abra `docs/index.html` localmente após `git pull` ou deixe o repo público.)

## Ajustes em `config.yaml`
- `alvo` por modelo → alerta quando o custo efetivo fica abaixo.
- `paginas` → cole URL de qualquer loja (BR ou CDE). Funciona em páginas com JSON-LD/metatag de preço.
- `paraguai` → frete, comissão do contato, cota e alíquota para comparar CDE x Brasil na mesma régua.
- `excluir_global` / `piso` → filtros contra peças, usados e anúncios-isca.

## Rodar local
    pip install -r requirements.txt && python radar.py

## Alertas
Dispara quando: custo ≤ alvo, novo mínimo histórico do modelo, ou queda ≥ 5% da mesma oferta.
Cooldown de 24h por oferta, a menos que caia de novo.

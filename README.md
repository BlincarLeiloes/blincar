# Blin Car

## Links

| | |
|---|---|
| **Site dos clientes** | https://blincarleiloes.github.io/blincar/ |
| **Agendamento (mandar ao cliente na conversa)** | https://blincarleiloes.github.io/blincar/agendar/ |
| **Painel dos administradores** | https://blincarleiloes.github.io/blincar/admin/ |
| **Ação: pintura geral ou R$ 2 mil no Pix (pré-venda)** | https://blincarleiloes.github.io/blincar/pre-venda/ |

### Links para divulgar

Cada link separa no painel de onde veio o cliente.

| Onde colocar | Link |
|---|---|
| Bio do Instagram | https://blincarleiloes.github.io/blincar/?origem=bio |
| Stories | https://blincarleiloes.github.io/blincar/?origem=stories |
| Status do WhatsApp | https://blincarleiloes.github.io/blincar/?origem=status |
| QR code impresso | https://blincarleiloes.github.io/blincar/?origem=qr |

## Arquivos

- `index.html`: página dos clientes, com pedido de orçamento pelo WhatsApp.
- `agendar/`: site de agendamento. O cliente faz o cadastro, escolhe retoque ou pintura geral e um dia com vaga, e paga o sinal pelo Pix.
- `avaliar/`: página onde o cliente avalia o serviço. O link chega por e-mail 5 dias depois da entrega (ou pelo WhatsApp, pelo painel).
- `pre-venda/`: página simples da ação "Pintura geral ou R$ 2 mil no Pix", com a arte, o valor dos bilhetes, como funciona e o botão "Compre aqui", que leva para o site da ação na Rifei. `arte.jpg` também é a imagem que aparece quando o link é mandado.
- `admin/`: painel dos administradores, com senha (métricas e agenda). O PDF do mês usa o jsPDF 4.2.1 (licença MIT), guardado em `admin/vendor/` e baixado só quando alguém pede o PDF.
- `config.js`: número do WhatsApp e link da planilha de métricas.
- `favicon.svg`, `favicon.ico`, `apple-touch-icon.png`, `icone-192.png`, `icone-512.png`: ícone da Blin Car na aba do navegador e no atalho do celular.
- `og.jpg`: imagem que aparece quando o link é mandado no WhatsApp ou no Instagram.

Blin Car · Estética automotiva · Vilar dos Teles, São João de Meriti - RJ

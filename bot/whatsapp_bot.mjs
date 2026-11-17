// Bot de WhatsApp sem navegador: envia para o dono cada mensagem que aparecer na pasta "outbox".
//
// Comandos (rode na pasta bot/):
//   node whatsapp_bot.mjs login    vincula o bot a uma conta do WhatsApp (QR em qr.png e no terminal), uma vez só
//   node whatsapp_bot.mjs run      fica rodando e envia os arquivos .json da outbox (use --dry para só listar)
//   node whatsapp_bot.mjs send "texto"   envia uma mensagem avulsa e sai
//   node whatsapp_bot.mjs status   mostra se há sessão salva e quantas mensagens esperam
//
// Cada arquivo da outbox é um JSON: {"mensagem": "texto", "tipo": "candidatura_enviada"}.
// Depois de enviado, o arquivo vai para "enviadas"; depois de várias falhas, para "falhas".
//
// A pasta auth/ guarda a sessão do WhatsApp (equivale a uma senha): não compartilhe nem versione.
// Aviso: usa a biblioteca não oficial Baileys. O WhatsApp não autoriza clientes não oficiais, e há risco
// de banimento da conta. Mantenha o volume baixo (apenas resumos para o próprio dono).

import fs from 'node:fs';
import http from 'node:http';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers } from '@whiskeysockets/baileys';
import QRCode from 'qrcode';
import pino from 'pino';

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(fs.readFileSync(process.env.BOT_CONFIG || path.join(AQUI, 'config.json'), 'utf-8'));
const PASTA_AUTH = path.join(AQUI, 'auth');
const PASTA_OUTBOX = path.resolve(AQUI, config.outbox);
const PASTA_ENVIADAS = path.resolve(AQUI, config.enviadas);
const PASTA_FALHAS = path.resolve(AQUI, config.falhas);
const DESTINO = `${config.destino}@s.whatsapp.net`;
const ARQUIVO_LOG = path.join(AQUI, 'bot.log');

// O endereço real do destino pode diferir do número digitado: em celulares do Brasil antigos o WhatsApp registra o
// número sem o 9 extra (55DD9XXXXXXXX vira 55DDXXXXXXXX). Mandar para o endereço errado dá "enviada" no servidor,
// mas a mensagem nunca chega. Por isso o bot pergunta ao WhatsApp qual é o endereço certo antes de enviar.
let destinoResolvido = null;
async function resolverDestino(sock) {
  if (destinoResolvido) return destinoResolvido;
  const semNove = config.destino.replace(/^(55\d{2})9(\d{8})$/, '$1$2');
  const candidatos = [...new Set([config.destino, semNove])];
  const achados = await sock.onWhatsApp(...candidatos);
  const real = achados?.find((item) => item.exists);
  if (!real) throw new Error(`destino ${config.destino} não encontrado no WhatsApp`);
  destinoResolvido = real.jid;
  log(`Destino resolvido: ${real.jid}`);
  return destinoResolvido;
}

const comando = process.argv[2] || 'run';
const modoSeco = process.argv.includes('--dry');

for (const pasta of [PASTA_OUTBOX, PASTA_ENVIADAS, PASTA_FALHAS]) fs.mkdirSync(pasta, { recursive: true });

function log(texto) {
  const linha = `${new Date().toISOString()} ${texto}`;
  console.log(linha);
  fs.appendFileSync(ARQUIVO_LOG, linha + '\n');
}

const dormir = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

// ---------- fila ----------

function listarOutbox() {
  return fs.readdirSync(PASTA_OUTBOX).filter((nome) => nome.endsWith('.json')).sort();
}

function lerItem(nome) {
  const caminho = path.join(PASTA_OUTBOX, nome);
  const item = JSON.parse(fs.readFileSync(caminho, 'utf-8'));
  if (typeof item.mensagem !== 'string' || !item.mensagem.trim()) throw new Error('campo "mensagem" ausente ou vazio');
  if (item.mensagem.length > 3500) throw new Error('mensagem acima de 3500 caracteres');
  return { caminho, item };
}

function mover(caminho, pastaDestino, extras = {}) {
  const alvo = path.join(pastaDestino, `${new Date().toISOString().replace(/[:.]/g, '-')}_${path.basename(caminho)}`);
  const conteudo = JSON.parse(fs.readFileSync(caminho, 'utf-8'));
  fs.writeFileSync(alvo, JSON.stringify({ ...conteudo, ...extras }, null, 2));
  fs.unlinkSync(caminho);
}

// Screenshots do worker de candidaturas: só vale imagem que esteja dentro de saida/aplicar (nunca um caminho qualquer).
const PASTA_IMAGENS = path.resolve(AQUI, config.imagens || '../prompts/saida/aplicar');
function imagemPermitida(caminho) {
  if (!caminho) return null;
  const real = path.resolve(PASTA_IMAGENS, caminho);
  if (!real.startsWith(PASTA_IMAGENS + path.sep) || !fs.existsSync(real)) return null;
  return real;
}

const enviosNaUltimaHora = [];
function podeEnviarAgora() {
  const corte = Date.now() - 3600 * 1000;
  while (enviosNaUltimaHora.length && enviosNaUltimaHora[0] < corte) enviosNaUltimaHora.shift();
  return enviosNaUltimaHora.length < config.max_mensagens_por_hora;
}

async function esvaziarOutbox(sock) {
  for (const nome of listarOutbox()) {
    if (!podeEnviarAgora()) {
      log('Limite de mensagens por hora atingido; o resto fica para a próxima rodada.');
      return;
    }
    let lido;
    try {
      lido = lerItem(nome);
    } catch (erro) {
      log(`Arquivo inválido ${nome}: ${erro.message}`);
      mover(path.join(PASTA_OUTBOX, nome), PASTA_FALHAS, { erro: erro.message });
      continue;
    }
    try {
      const imagem = imagemPermitida(lido.item.imagem);
      const conteudo = imagem ? { image: fs.readFileSync(imagem), caption: lido.item.mensagem } : { text: lido.item.mensagem };
      const resposta = await sock.sendMessage(await resolverDestino(sock), conteudo);
      enviosNaUltimaHora.push(Date.now());
      mover(lido.caminho, PASTA_ENVIADAS, { enviada_em: new Date().toISOString(), id_whatsapp: resposta?.key?.id ?? null });
      log(`Enviada: ${nome} (${lido.item.tipo ?? 'sem tipo'})`);
    } catch (erro) {
      const tentativas = (lido.item.tentativas ?? 0) + 1;
      if (tentativas >= config.max_tentativas) {
        log(`Falhou ${tentativas} vezes, movida para falhas: ${nome} (${erro.message})`);
        mover(lido.caminho, PASTA_FALHAS, { tentativas, erro: erro.message });
      } else {
        fs.writeFileSync(lido.caminho, JSON.stringify({ ...lido.item, tentativas }, null, 2));
        log(`Falha ao enviar ${nome} (tentativa ${tentativas}): ${erro.message}`);
      }
    }
    await dormir(config.pausa_entre_mensagens_segundos * 1000 + Math.floor(Math.random() * 1500));
  }
}

// ---------- mensagens recebidas ----------

// As respostas do dono (decisões sobre as vagas em revisão) viram arquivos em "inbox", que a automação lê depois.
// "do_dono" só é verdadeiro quando o número do remetente termina com os 8 últimos dígitos do destino configurado.
const PASTA_INBOX = path.resolve(AQUI, config.inbox || '../prompts/saida/inbox');
fs.mkdirSync(PASTA_INBOX, { recursive: true });

function guardarRecebidas(mensagens, tipo) {
  // "append" é como o WhatsApp costuma entregar o que chegou com o PC desligado; aceita só as das últimas 24 h e sem repetir.
  if (tipo !== 'notify' && tipo !== 'append') return;
  for (const msg of mensagens) {
    const jid = msg.key?.remoteJid || '';
    if (msg.key?.fromMe || jid.endsWith('@g.us') || jid === 'status@broadcast') continue;
    const texto = msg.message?.conversation || msg.message?.extendedTextMessage?.text || '';
    if (!texto.trim()) continue;
    const segundos = Number(msg.messageTimestamp?.toString?.() ?? msg.messageTimestamp ?? 0);
    const enviadaEm = segundos ? new Date(segundos * 1000).toISOString() : null;
    if (tipo === 'append' && (!segundos || Date.now() - segundos * 1000 > 24 * 3600 * 1000)) continue;
    const id12 = (msg.key.id || 'sem-id').slice(0, 12);
    if (fs.readdirSync(PASTA_INBOX).some((arquivo) => arquivo.endsWith(`_${id12}.json`))) continue;
    // O WhatsApp novo entrega o remetente como "<número>@lid"; por isso "donos" lista o endereço conhecido do dono.
    const doDono = (config.donos || []).includes(jid) || jid.split('@')[0].endsWith(config.destino.slice(-8));
    const nome = `${new Date().toISOString().replace(/[:.]/g, '-')}_${(msg.key.id || 'sem-id').slice(0, 12)}.json`;
    fs.writeFileSync(path.join(PASTA_INBOX, nome), JSON.stringify({
      texto, de: jid, do_dono: doDono, recebida_em: new Date().toISOString(), enviada_em: enviadaEm, id_whatsapp: msg.key.id ?? null, processada: false,
    }, null, 2));
    log(`Recebida (${doDono ? 'do dono' : 'de outro número'}): ${nome}`);
    if (doDono && config.conversar) chamarConversa();
  }
}

// O script lê a inbox inteira e responde pela outbox; a trava dele evita duas execuções ao mesmo tempo.
// A cada minuto o bot chama a fila de candidaturas (inicia pedidos, trata cota e prazos). A trava do script evita duas ao mesmo tempo.
function chamarFila() {
  if (!config.fila) return;
  const script = path.resolve(AQUI, config.fila);
  const filho = spawn('python', [script, 'tick'], { cwd: path.dirname(script), detached: true, stdio: 'ignore', windowsHide: true });
  filho.on('error', (erro) => log(`Falha ao chamar a fila: ${erro.message}`));
  filho.unref();
}

function chamarConversa() {
  const script = path.resolve(AQUI, config.conversar);
  const filho = spawn('python', [script], { cwd: path.dirname(script), detached: true, stdio: 'ignore', windowsHide: true });
  filho.on('error', (erro) => log(`Falha ao chamar a conversa: ${erro.message}`));
  filho.unref();
}

// ---------- conexão ----------

async function mostrarQr(qr) {
  await QRCode.toFile(path.join(AQUI, 'qr.png'), qr, { scale: 8 });
  console.log(await QRCode.toString(qr, { type: 'terminal', small: true }));
  log('QR code atualizado em bot/qr.png. No celular: WhatsApp, Dispositivos conectados, Conectar dispositivo.');
}

// Página local (só neste computador) que mostra o QR e se atualiza sozinha: o código do WhatsApp muda a cada ~20 s.
function iniciarServidorQr() {
  const pagina = '<!doctype html><meta charset="utf-8"><title>QR do bot</title>'
    + '<body style="font-family:sans-serif;text-align:center;margin-top:30px"><h2>Escaneie com o WhatsApp</h2>'
    + '<p>No celular: WhatsApp, Dispositivos conectados, Conectar dispositivo.</p>'
    + '<img id="q" src="/qr.png" width="360" alt="aguardando o QR">'
    + '<script>setInterval(function(){document.getElementById("q").src="/qr.png?t="+Date.now()},3000)</script></body>';
  const servidor = http.createServer((req, res) => {
    if (req.url.startsWith('/qr.png')) {
      try {
        const png = fs.readFileSync(path.join(AQUI, 'qr.png'));
        res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
        res.end(png);
      } catch {
        res.writeHead(404);
        res.end();
      }
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(pagina);
  });
  servidor.listen(8765, '127.0.0.1');
  log('Página do QR em http://127.0.0.1:8765');
  return servidor;
}

async function conectar(aoAbrir) {
  const { state, saveCreds } = await useMultiFileAuthState(PASTA_AUTH);
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    version, auth: state, logger: pino({ level: 'silent' }), browser: Browsers.macOS('Candidaturas'),
    markOnlineOnConnect: false, syncFullHistory: false,
  });
  sock.ev.on('creds.update', saveCreds);
  if (comando === 'run') sock.ev.on('messages.upsert', ({ messages, type }) => guardarRecebidas(messages, type));
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) await mostrarQr(qr);
    if (connection === 'open') {
      log('Conectado ao WhatsApp.');
      await aoAbrir(sock);
    }
    if (connection === 'close') {
      const codigo = lastDisconnect?.error?.output?.statusCode;
      if (codigo === DisconnectReason.loggedOut) {
        log('Sessão encerrada pelo celular. Rode "node whatsapp_bot.mjs login" de novo.');
        process.exit(3);
      }
      log(`Conexão fechada (código ${codigo ?? 'desconhecido'}); reconectando em 5 segundos.`);
      await dormir(5000);
      await conectar(aoAbrir);
    }
  });
}

// ---------- comandos ----------

if (comando === 'status') {
  const temSessao = fs.existsSync(path.join(PASTA_AUTH, 'creds.json'));
  console.log(JSON.stringify({ sessao_salva: temSessao, na_outbox: listarOutbox().length, enviadas: fs.readdirSync(PASTA_ENVIADAS).length, falhas: fs.readdirSync(PASTA_FALHAS).length }));
} else if (comando === 'login') {
  const servidorQr = iniciarServidorQr();
  await conectar(async () => {
    log('Login concluído. A sessão está salva em bot/auth.');
    servidorQr.close();
    if (fs.existsSync(path.join(AQUI, 'qr.png'))) fs.unlinkSync(path.join(AQUI, 'qr.png'));
    await dormir(4000);
    process.exit(0);
  });
} else if (comando === 'send') {
  const texto = process.argv.slice(3).join(' ').trim();
  if (!texto) { console.error('Uso: node whatsapp_bot.mjs send "texto"'); process.exit(2); }
  await conectar(async (sock) => {
    await sock.sendMessage(await resolverDestino(sock), { text: texto });
    log('Mensagem avulsa enviada.');
    await dormir(3000);
    process.exit(0);
  });
} else if (comando === 'run' && modoSeco) {
  for (const nome of listarOutbox()) {
    try { console.log(`[dry] ${nome}: ${lerItem(nome).item.mensagem.split('\n')[0]}`); } catch (erro) { console.log(`[dry] ${nome}: INVÁLIDO (${erro.message})`); }
  }
  console.log(`[dry] ${listarOutbox().length} arquivo(s) na outbox; nada foi enviado.`);
} else if (comando === 'run') {
  if (!fs.existsSync(path.join(PASTA_AUTH, 'creds.json'))) {
    console.error('Sem sessão salva. Rode primeiro: node whatsapp_bot.mjs login');
    process.exit(2);
  }
  let rodando = false;
  let socketAtual = null;
  await conectar(async (sock) => {
    socketAtual = sock; // depois de uma reconexão o laço passa a usar o socket novo
    if (rodando) return;
    rodando = true;
    log(`Aguardando mensagens na outbox (${PASTA_OUTBOX}).`);
    setInterval(chamarFila, 60 * 1000);
    chamarFila(); // ao subir: recupera pedidos que ficaram no meio e vê se a cota já voltou
    for (;;) {
      try { await esvaziarOutbox(socketAtual); } catch (erro) { log(`Erro no laço: ${erro.message}`); }
      await dormir(config.intervalo_segundos * 1000);
    }
  });
} else {
  console.error('Comando desconhecido. Use: login, run, send, status.');
  process.exit(2);
}

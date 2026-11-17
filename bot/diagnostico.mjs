// Diagnóstico do bot: mostra com qual conta o bot está logado e para qual endereço do WhatsApp o destino resolve.
// Uso (com o bot parado, porque duas conexões com a mesma sessão se derrubam): node diagnostico.mjs [--enviar]
//   --enviar   manda uma mensagem de teste ao endereço resolvido
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import makeWASocket, { useMultiFileAuthState, fetchLatestBaileysVersion, Browsers } from '@whiskeysockets/baileys';
import pino from 'pino';

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(fs.readFileSync(path.join(AQUI, 'config.json'), 'utf-8'));
const enviar = process.argv.includes('--enviar');

const { state, saveCreds } = await useMultiFileAuthState(path.join(AQUI, 'auth'));
const { version } = await fetchLatestBaileysVersion();
const sock = makeWASocket({ version, auth: state, logger: pino({ level: 'silent' }), browser: Browsers.macOS('Candidaturas'), markOnlineOnConnect: false });
sock.ev.on('creds.update', saveCreds);

sock.ev.on('connection.update', async ({ connection }) => {
  if (connection !== 'open') return;
  console.log('Conectado. Conta do bot (user):', JSON.stringify(sock.user));
  const numero = config.destino;
  const semNove = numero.replace(/^(55\d{2})9(\d{8})$/, '$1$2');
  const candidatos = [...new Set([numero, semNove])];
  console.log('Candidatos a destino:', candidatos.join(', '));
  const achados = await sock.onWhatsApp(...candidatos);
  console.log('Resultado de onWhatsApp:', JSON.stringify(achados));
  const real = achados?.find((item) => item.exists);
  console.log('Endereço real do destino:', real ? real.jid : 'NÃO ENCONTRADO');
  console.log('Endereço que o bot usava:', `${numero}@s.whatsapp.net`);
  if (enviar && real) {
    const resposta = await sock.sendMessage(real.jid, { text: 'Teste do bot de candidaturas: se você leu isto, o envio funciona.' });
    console.log('Mensagem de teste enviada, id:', resposta?.key?.id);
  }
  setTimeout(() => process.exit(0), 4000);
});

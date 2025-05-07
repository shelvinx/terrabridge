// file: static/ws-client.js

export default function initWebSocket({ onMessage }) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.onmessage = e => onMessage(JSON.parse(e.data));
  ws.onerror   = () => console.error("ws error");
  ws.onclose   = () => console.warn("ws closed");
}
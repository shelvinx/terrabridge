// file: static/ws-client.js

export default function initWebSocket({ onMessage }) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${location.host}/ws`;
  let ws;
  let reconnectAttempts = 0;
  let reconnectTimeout;
  const maxReconnectAttempts = 5;
  const reconnectDelay = 2000; // 2 seconds base delay

  function connect() {
    console.log(`Connecting to WebSocket: ${wsUrl}`);
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
      console.log('WebSocket connection established');
      reconnectAttempts = 0; // Reset reconnect counter on successful connection
      onMessage({ status: 'connected' });
    };
    
    ws.onmessage = (e) => {
      console.log('WebSocket message received:', e.data);
      try {
        const data = JSON.parse(e.data);
        onMessage(data);
      } catch (err) {
        console.error('Error parsing WebSocket message:', err);
        onMessage({ error: 'Failed to parse WebSocket message' });
      }
    };
    
    ws.onerror = (e) => {
      console.error('WebSocket error:', e);
      onMessage({ error: 'WebSocket connection error' });
    };
    
    ws.onclose = (e) => {
      console.warn(`WebSocket closed (code: ${e.code}, reason: ${e.reason || 'none provided'})`);
      onMessage({ status: 'disconnected' });
      
      // Attempt to reconnect if the connection was closed unexpectedly
      if (reconnectAttempts < maxReconnectAttempts) {
        const delay = reconnectDelay * Math.pow(1.5, reconnectAttempts);
        console.log(`Attempting to reconnect in ${delay}ms (attempt ${reconnectAttempts + 1}/${maxReconnectAttempts})`);
        reconnectTimeout = setTimeout(() => {
          reconnectAttempts++;
          connect();
        }, delay);
      } else {
        console.error('Maximum reconnection attempts reached');
        onMessage({ error: 'Failed to establish WebSocket connection after multiple attempts' });
      }
    };
  }
  
  // Initial connection
  connect();
  
  // Return a method to manually reconnect and cleanup
  return {
    reconnect: () => {
      if (ws) {
        ws.close();
      }
      clearTimeout(reconnectTimeout);
      reconnectAttempts = 0;
      connect();
    },
    close: () => {
      clearTimeout(reconnectTimeout);
      if (ws) {
        ws.close();
      }
    }
  };
}
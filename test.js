const { createApp, ref, reactive, computed, onMounted, onUnmounted, nextTick } = Vue;

createApp({
    setup() {
        const error = ref('');
        const currentCurrency = ref('USD');
        const twdRate = ref(32.5);
        const logs = ref([]);
        const consoleRef = ref(null);
        const isPlacingOrder = ref(false);
        const trades = ref([]);
        const liveTrades = ref([]);
        const tempTradeAmount = ref(150);
        const limitPrice = ref('');
        const openOrders = ref([]);
        const selectedTf = ref('1m');
        
        const showHistoryNotebook = ref(false);
        const historySummaries = ref([]);

        // Limit order panel state
        const loSide = ref('long');
        const loOrderType = ref('market');
        const loTpPct = ref('');
        const loSlPct = ref('');
        const timeframes = [
            { label: '1m', value: '1m' },
            { label: '5m', value: '5m' },
            { label: '15m', value: '15m' },
            { label: '1h', value: '1h' },
        ];

        const position = ref({ asset: '-', qty: 0, avg_price: 0, total_cost: 0, current_price: 0, current_value: 0, pnl: 0, pnl_percent: 0, realized_pnl: 0 });

        const customSymbol = ref('');
        const addCustomCoin = () => {
            let s = customSymbol.value.trim().toUpperCase();
            if (!s) return;
            if (!s.endsWith('USDT')) s += 'USDT';
            if (!coins.value.find(c => c.symbol === s)) {
                coins.value.push({ symbol: s, name: s.replace('USDT',''), label: '自訂', price: 0, prevPrice: 0, priceClass: '' });
                // Subscribe to ticker immediately
                try {
                    const ws = new WebSocket(`wss://stream.binance.com:9443/ws/${s.toLowerCase()}@ticker`);
                    ws.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        const c = coins.value.find(x => x.symbol === s);
                        if (c) {
                            c.prevPrice = c.price; c.price = parseFloat(data.c);
                            if (c.prevPrice > 0) {
                                c.priceClass = c.price > c.prevPrice ? 'up' : c.price < c.prevPrice ? 'down' : '';
                                setTimeout(() => { c.priceClass = ''; }, 400);
                            }
                        }
                    };
                } catch(e){}
            }
            setActiveCoin(s);
            customSymbol.value = '';
        };
        const coins = ref([
            { symbol: 'XRPUSDT', name: 'XRP', label: 'XRP', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'DOGEUSDT', name: 'DOGE', label: 'DOGE', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'ADAUSDT', name: 'ADA', label: 'ADA', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'LINKUSDT', name: 'LINK', label: 'LINK', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'AVAXUSDT', name: 'AVAX', label: 'AVAX', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'DOTUSDT', name: 'DOT', label: 'DOT', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'UNIUSDT', name: 'UNI', label: 'UNI', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'NEARUSDT', name: 'NEAR', label: 'NEAR', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'FETUSDT', name: 'FET', label: 'FET', price: 0, prevPrice: 0, priceClass: '' },
            { symbol: 'SUIUSDT', name: 'SUI', label: 'SUI', price: 0, prevPrice: 0, priceClass: '' }
        ]);
        const activeSymbol = ref(localStorage.getItem('activeSymbol') || 'SUIUSDT');
        const showAllTradesModal = ref(false);
        const allTradesHistory = ref([]);

        const showLogs = ref(false);

        const bot = ref({ is_running: false, strategy: '-', balance_quote: 0, active_orders: 0, regime: '-' });
        const allPositions = ref({});

        // ── Volume bars ───────────────────────────────────────────
        const volumeBars = ref([]);
        const latestVolumeStr = ref('--');
        const volumeUnit = ref('');
        const high24h = ref(0);
        const low24h = ref(0);
        const klinesData = ref([]);

        const buildVolumeBars = (data) => {
            if (!data || data.length === 0) return;
            klinesData.value = data;
            const maxVol = Math.max(...data.map(d => d.volume));
            volumeBars.value = data.map(d => ({
                pct: maxVol > 0 ? Math.max(4, (d.volume / maxVol) * 100) : 4,
                bull: d.close >= d.open,
                label: `Vol: ${d.volume.toLocaleString(undefined, {maximumFractionDigits: 2})}`
            }));
            const last = data[data.length - 1];
            if (last) {
                const v = last.volume;
                if (v >= 1000) {
                    latestVolumeStr.value = (v / 1000).toFixed(2) + 'K';
                } else {
                    latestVolumeStr.value = v.toFixed(2);
                }
                volumeUnit.value = activeSymbol.value.replace(/USDT|BNB|BUSD|BTC|ETH/, '');
            }
        };

        // ── Lightweight Charts candlestick ────────────────────────
        let chart = null;
        let candleSeries = null;
        let volSeries = null;
        let bbUpperSeries = null;
        let bbMiddleSeries = null;
        let bbLowerSeries = null;

        const initChart = () => {
            const container = document.getElementById('chart-container');
            if (!container) return;
            if (chart) { chart.remove(); chart = null; }

            chart = LightweightCharts.createChart(container, {
                layout: {
                    background: { color: 'transparent' },
                    textColor: '#6b7280',
                },
                grid: {
                    vertLines: { color: 'rgba(0,0,0,0.06)' },
                    horzLines: { color: 'rgba(0,0,0,0.06)' },
                },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor: 'rgba(0,0,0,0.1)' },
                timeScale: {
                    borderColor: 'rgba(0,0,0,0.1)',
                    timeVisible: true,
                    secondsVisible: false,
                },
                handleScroll: true,
                handleScale: true,
            });

            candleSeries = chart.addCandlestickSeries({
                upColor: '#0ecb81',
                downColor: '#f6465d',
                borderUpColor: '#0ecb81',
                borderDownColor: '#f6465d',
                wickUpColor: '#0ecb81',
                wickDownColor: '#f6465d',
            });

            bbUpperSeries = chart.addLineSeries({ color: 'rgba(246,70,93,0.3)', lineWidth: 1, lastValueVisible: false });
            bbMiddleSeries = chart.addLineSeries({ color: 'rgba(132,142,156,0.4)', lineWidth: 1, lastValueVisible: false });
            bbLowerSeries = chart.addLineSeries({ color: 'rgba(14,203,129,0.3)', lineWidth: 1, lastValueVisible: false });

            // Auto-resize
            const ro = new ResizeObserver(() => {
                if (chart && container) {
                    chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
                }
            });
            ro.observe(container);
            chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
        };

        const loadKlines = async () => {
            try {
                const res = await fetch(`/api/klines/${activeSymbol.value}?interval=${selectedTf.value}&limit=80`);
                if (!res.ok) throw new Error();
                const resp = await res.json();
                const data = resp.data || resp;
                buildVolumeBars(data);
                if (candleSeries) {
                    const candles = data.map(d => ({
                        time: Math.floor(d.time),
                        open: d.open, high: d.high, low: d.low, close: d.close
                    }));
                    candleSeries.setData(candles);
                    chart?.timeScale().fitContent();

                    // 布林通道計算
                    const closes = data.map(d => d.close);
                    const period = 20;
                    if (closes.length >= period) {
                        const bbData = [];
                        for (let i = period - 1; i < closes.length; i++) {
                            const slice = closes.slice(i - period + 1, i + 1);
                            const sma = slice.reduce((a, b) => a + b, 0) / period;
                            const variance = slice.reduce((s, v) => s + (v - sma) ** 2, 0) / period;
                            const std = Math.sqrt(variance);
                            bbData.push({
                                time: Math.floor(data[i].time),
                                mid: sma,
                                upper: sma + 2 * std,
                                lower: sma - 2 * std,
                            });
                        }
                        if (bbUpperSeries) {
                            bbUpperSeries.setData(bbData.map(d => ({ time: d.time, value: d.upper })));
                            bbMiddleSeries.setData(bbData.map(d => ({ time: d.time, value: d.mid })));
                            bbLowerSeries.setData(bbData.map(d => ({ time: d.time, value: d.lower })));
                        }
                    }
                }
            } catch (e) {
                console.error('K線載入失敗', e);
            }
        };

        const changeTf = (tf) => {
            selectedTf.value = tf;
            loadKlines();
        };

        // Update latest candle from WS ticker
        const updateLatestCandle = (symbol, price) => {
            if (symbol !== activeSymbol.value || !candleSeries) return;
            const now = Math.floor(Date.now() / 1000);
            try {
                candleSeries.update({ time: now, open: price, high: price, low: price, close: price });
            } catch(e) {}
        };

        const API_BASE = '/api';

        const activeCoin = computed(() => coins.value.find(c => c.symbol === activeSymbol.value));

        const sortedCoins = computed(() => {
            return [...coins.value].sort((a, b) => {
                const hasA = allPositions.value[a.symbol] ? 1 : 0;
                const hasB = allPositions.value[b.symbol] ? 1 : 0;
                if (hasA !== hasB) {
                    return hasB - hasA;
                }
                return a.symbol.localeCompare(b.symbol);
            });
        });

        const formattedMainPrice = computed(() => {
            if (!activeCoin.value?.price) return '---.--';
            const p = currentCurrency.value === 'TWD' ? activeCoin.value.price * twdRate.value : activeCoin.value.price;
            const absV = Math.abs(p);
            let d = 2;
            if (absV < 0.0001) d = 8;
            else if (absV < 1) d = 6;
            else if (absV < 1000) d = 4;
            else d = 2;
            return p.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
        });

        const mainPriceClass = computed(() => activeCoin.value?.priceClass || '');
        const currentCurrencySymbol = computed(() => currentCurrency.value === 'TWD' ? 'NT$' : '$');

        const formatMiniPrice = (price) => {
            if (!price) return '0.00';
            const p = currentCurrency.value === 'TWD' ? price * twdRate.value : price;
            const absV = Math.abs(p);
            let d = 2;
            if (absV < 0.0001) d = 8;
            else if (absV < 1) d = 6;
            else if (absV < 1000) d = 4;
            else d = 2;
            return p.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
        };

        const formatCurrencyVal = (val) => {
            if (!val) return '0.00';
            const v = currentCurrency.value === 'TWD' ? val * twdRate.value : val;
            const absV = Math.abs(v);
            let d = 2;
            if (absV < 0.0001) d = 8;
            else if (absV < 1) d = 6;
            else if (absV < 1000) d = 4;
            else d = 2;
            return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
        };

        const getActiveCoinLabel = () => activeCoin.value ? `${activeCoin.value.name} (${activeCoin.value.label})` : '';

        const getQuoteAsset = () => {
            const s = activeSymbol.value;
            if (s.endsWith('BNB')) return 'BNB';
            if (s.endsWith('USDT')) return 'USDT';
            if (s.endsWith('BUSD')) return 'BUSD';
            return 'USDT';
        };

        const formatQuoteBalance = () => {
            const b = bot.value.balance_quote || 0;
            const q = getQuoteAsset();
            return b.toLocaleString(undefined, { minimumFractionDigits: q === 'BNB' ? 4 : 2, maximumFractionDigits: q === 'BNB' ? 4 : 2 }) + ' ' + q;
        };

        const formatTradeTime = (ts) => {
            if (!ts) return '-';
            // ts is already in milliseconds from paper_state.json / backend
            return new Date(ts).toLocaleString('zh-TW', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        };

        const formatLiveTradeTime = (ts) => {
            if (!ts) return '-';
            return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        };

        const addLog = (text, level = 'info') => {
            const t = new Date().toTimeString().split(' ')[0];
            let lc = 'log-info';
            if (level === 'warning') lc = 'log-warning';
            if (level === 'danger')  lc = 'log-danger';
            if (level === 'success') lc = 'log-success';
            logs.value.push({ time: t, text, levelClass: lc });
            if (logs.value.length > 100) logs.value.shift();
            nextTick(() => { if (consoleRef.value) consoleRef.value.scrollTop = consoleRef.value.scrollHeight; });
        };

        const clearLogs = () => { logs.value = []; addLog('控制台已清空。'); };
        const copyLogs = async () => {
            if (!logs.value.length) {
                showToast('目前沒有系統紀錄可複製', 'warning');
                return;
            }
            const payload = logs.value.map(l => `[${l.time}] ${l.text}`).join('\n');
            
            let success = false;
            try {
                const textarea = document.createElement('textarea');
                textarea.value = payload;
                textarea.setAttribute('readonly', '');
                textarea.style.position = 'fixed';
                textarea.style.left = '-9999px';
                document.body.appendChild(textarea);
                textarea.select();
                textarea.setSelectionRange(0, textarea.value.length);
                success = document.execCommand('copy');
                document.body.removeChild(textarea);
            } catch (e) {}

            if (success) {
                showToast('系統紀錄已複製到剪貼簿', 'success');
                return;
            }

            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(payload);
                    showToast('系統紀錄已複製到剪貼簿', 'success');
                } else {
                    throw new Error('No clipboard API');
                }
            } catch (e) {
                showToast('複製失敗，請手動選取內容', 'danger');
            }
        };
        const toggleCurrency = () => { currentCurrency.value = currentCurrency.value === 'USD' ? 'TWD' : 'USD'; };

        // ── Toast notification ──────────────────────────────────
        const toast = reactive({ show: false, text: '', type: 'info' });
        let toastTimer = null;
        const showToast = (text, type = 'info', duration = 3000) => {
            toast.text = text;
            toast.type = type;
            toast.show = true;
            if (toastTimer) clearTimeout(toastTimer);
            toastTimer = setTimeout(() => { toast.show = false; }, duration);
        };

        let liveTradesWs = null;
        const initLiveTradesWs = () => {
            if (liveTradesWs) {
                try { liveTradesWs.close(); } catch(e) {}
            }
            liveTrades.value = []; // 清空舊的成交紀錄
            if (!activeSymbol.value) return;
            
            const sym = activeSymbol.value.toLowerCase();
            liveTradesWs = new WebSocket(`wss://fstream.binance.com/ws/${sym}@aggTrade`);
            liveTradesWs.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.e === 'aggTrade') {
                        // 只需要最新的 50 筆
                        liveTrades.value.unshift({
                            id: data.a,
                            price: parseFloat(data.p),
                            qty: parseFloat(data.q),
                            time: data.T,
                            is_buy: !data.m // m=true 意思是買方是造市商(被動), 代表這筆是主動賣出(賣單吃單); 所以 !m 就是主動買入
                        });
                        if (liveTrades.value.length > 50) {
                            liveTrades.value.pop();
                        }
                    }
                } catch(e) {}
            };
        };

        const setActiveCoin = async (symbol) => {
            activeSymbol.value = symbol;
            localStorage.setItem('activeSymbol', symbol);
            addLog(`切換檢視: ${coins.value.find(c => c.symbol === symbol)?.name}`);
            fetchPosition(); fetchTrades(); loadKlines(); initLiveTradesWs();
        };

        const fetchExchangeRate = async () => {
            try {
                const res = await fetch(`${API_BASE}/exchangerate/usdtwd`);
                const resp = await res.json();
                const data = resp.data || resp;
                twdRate.value = data.rate;
            } catch(e) {}
        };

        const fetchBotStatus = async () => {
            try {
                const res = await fetch(`${API_BASE}/bot-status`);
                const resp = await res.json();
                const data = resp.data || resp;
                if (bot.value.is_running !== data.is_running && bot.value.strategy !== '-')
                    addLog(`機器人狀態: ${data.is_running ? '🚀 啟動' : '⏹️ 停止'}`);
                bot.value = data;
                
                // 動態更新界面上的監控幣種
                if (data.active_symbols && data.active_symbols.length > 0) {
                    const newCoins = data.active_symbols.map(sym => {
                        const existing = coins.value.find(c => c.symbol === sym);
                        if (existing) return existing;
                        const name = sym.replace('USDT', '');
                        return { symbol: sym, name: name, label: name, price: 0, prevPrice: 0, priceClass: '' };
                    });
                    
                    const oldSyms = coins.value.map(c => c.symbol).join(',');
                    const newSyms = newCoins.map(c => c.symbol).join(',');
                    if (oldSyms !== newSyms) {
                        coins.value = newCoins;
                        if (!newCoins.some(c => c.symbol === activeSymbol.value)) {
                            setActiveCoin(newCoins[0].symbol);
                        } else {
                            initWebSocket();
                        }
                    }
                }

                if (data.trade_amount !== undefined) {
                    if (document.activeElement?.type !== 'number' && document.activeElement?.type !== 'range')
                        tempTradeAmount.value = data.trade_amount;
                }
                if (error.value) error.value = '';
            } catch(e) { error.value = '後端連線失敗。'; }
        };

        const updateTradeAmount = async () => {
            let a = Math.max(0, Math.min(150, tempTradeAmount.value));
            tempTradeAmount.value = a;
            try {
                const res = await fetch(`${API_BASE}/bot-status/set-amount/${a}`, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok) addLog(`交易金額設為 ${data.trade_amount} ${getQuoteAsset()}`, 'success');
            } catch(e) { addLog('設定金額失敗', 'danger'); }
        };

        const fetchPosition = async () => {
            try {
                const res = await fetch(`${API_BASE}/position/${activeSymbol.value}`);
                if (res.ok) position.value = await res.json();
            } catch (err) { }
        };

        const fetchAllPositions = async () => {
            try {
                const res = await fetch('/api/positions');
                const data = await res.json();
                // 過濾掉數量為 0 的倉位
                const filtered = {};
                for (let sym in data) {
                    if (Math.abs(data[sym].qty) > 0) {
                        filtered[sym] = data[sym];
                    }
                }
                allPositions.value = filtered;
            } catch (err) {}
        };

        const fetchTrades = async () => {
            try {
                const res = await fetch(`${API_BASE}/trades/ALL`);
                if (res.ok) {
                    const data = await res.json();
                    trades.value = data.sort((a, b) => b.time - a.time);
                }
            } catch(e) {}
        };

        const fetchOpenOrders = async () => {
            try {
                const sym = activeSymbol.value;
                if (!sym) return;
                const res = await fetch(`${API_BASE}/open-orders?symbol=${sym}`);
                if (!res.ok) return;
                const data = await res.json();
                openOrders.value = (data.orders || []).map(o => ({
                    orderId: o.orderId,
                    side: o.side,
                    price: parseFloat(o.price) || 0,
                    origQty: parseFloat(o.origQty) || 0,
                    type: o.type
                }));
            } catch(e) {}
        };

        const autoSnipeBestCoin = async () => {
            if (isPlacingOrder.value) return;
            isPlacingOrder.value = true;
            addLog("⚡ 智能狙擊：正在掃描全市場波動最大的幣種...", "warning");
            try {
                const res = await fetch(`${API_BASE}/radar/scan`);
                if (res.ok) {
                    const resp = await res.json();
                const data = resp.data || resp;
                    if (data.status === 'success') {
                        if (data.cooldown) {
                            addLog(`⏳ 冷卻中，請等 ${data.cooldown} 秒後再試`, "warning");
                        } else if (data.best_symbol) {
                            if (data.best_symbol === activeSymbol.value) {
                                addLog(`✅ 當前 ${data.best_symbol} 仍是最飆幣種，無需切換`, "success");
                            } else {
                                addLog(`🎯 狙擊成功！已切換至最飆幣種：${data.best_symbol}`, "success");
                                activeSymbol.value = data.best_symbol;
                                localStorage.setItem('activeSymbol', data.best_symbol);
                                initChart();
                                for (const w of wsConnections) { try { w.close(); } catch(e) {} }
                                wsConnections.length = 0;
                                initWebSocket();
            initLiveTradesWs();
                            }
                        } else {
                            addLog("⚠️ 掃描完成但無合適目標", "warning");
                        }
                    } else {
                        addLog(`⚠️ 掃描失敗: ${data.detail || data.msg}`, "warning");
                    }
                } else {
                    addLog(`❌ 掃描請求失敗 (${res.status})`, "danger");
                }
            } catch (e) {
                addLog(`❌ 掃描發生錯誤: ${e.message}`, "danger");
            } finally {
                isPlacingOrder.value = false;
            }
        };

        const cancelAllOrders = async () => {
            try {
                const res = await fetch(`${API_BASE}/trades/ALL`);
                if (res.ok) trades.value = await res.json();
            } catch(e) {}
        };

        // Cancel open orders for the active symbol (called from template)
        const cancelOrders = async () => {
            const sym = activeSymbol.value;
            if (!sym) return;
            try {
                const res = await fetch(`${API_BASE}/open-orders/${sym}`, { method: 'DELETE' });
                const data = await res.json();
                if (res.ok) {
                    addLog(`✅ 已取消 ${sym} 所有掛單`, 'success');
                    openOrders.value = [];
                } else {
                    addLog(`⚠️ 取消掛單失敗: ${data.detail || ''}`, 'warning');
                }
            } catch(e) { addLog(`❌ 取消掛單錯誤: ${e.message}`, 'danger'); }
        };

        const toggleBot = async () => {
            try {
                const res = await fetch(`${API_BASE}/bot-status/toggle`, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (data.status === 'success') { bot.value.is_running = data.is_running; addLog(`手動${data.is_running ? '啟動' : '停止'}機器人`); }
            } catch(e) { addLog('開關失敗', 'warning'); }
        };

        const placeMarketBuy = async () => {
            const coin = activeCoin.value; if (!coin) return;
            isPlacingOrder.value = true;
            const priceVal = parseFloat(limitPrice.value) || 0;
            const isLimit = priceVal > 0;
            const orderTypeStr = isLimit ? `限價(${priceVal})` : '市價';
            
            if (position.value.qty < 0) {
                addLog(`🔄 先平空單，再開多...`);
                try { 
                    const endpoint = isLimit ? `${API_BASE}/order/limit-sell/${coin.symbol}?price=${priceVal}` : `${API_BASE}/order/market-sell/${coin.symbol}`;
                    await fetch(endpoint, { method: 'POST' }); 
                } catch(e) {}
                await new Promise(r => setTimeout(r, 600));
            }
            addLog(`🛒 ${orderTypeStr}買入 ${tempTradeAmount.value} USDT 的 ${coin.name}...`);
            try {
                const endpoint = isLimit ? `${API_BASE}/order/limit-buy/${coin.symbol}?amount=${tempTradeAmount.value}&price=${priceVal}` : `${API_BASE}/order/market-buy/${coin.symbol}?amount=${tempTradeAmount.value}`;
                const res = await fetch(endpoint, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && data.status === 'success') {
                    addLog(`✅ 買入成功! ID:${data.order.orderId} 數量:${parseFloat(data.order.executedQty).toFixed(5)}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) { addLog(`❌ 買入失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; limitPrice.value = ''; }
        };

        const placeMarketShort = async () => {
            const coin = activeCoin.value; if (!coin) return;
            isPlacingOrder.value = true;
            const priceVal = parseFloat(limitPrice.value) || 0;
            const isLimit = priceVal > 0;
            const orderTypeStr = isLimit ? `限價(${priceVal})` : '市價';

            if (position.value.qty > 0) {
                addLog(`🔄 先平多單，再開空...`);
                try { 
                    const endpoint = isLimit ? `${API_BASE}/order/limit-sell/${coin.symbol}?price=${priceVal}` : `${API_BASE}/order/market-sell/${coin.symbol}`;
                    await fetch(endpoint, { method: 'POST' }); 
                } catch(e) {}
                await new Promise(r => setTimeout(r, 600));
            }
            addLog(`🛒 ${orderTypeStr}做空 ${tempTradeAmount.value} USDT 的 ${coin.name}...`);
            try {
                const endpoint = isLimit ? `${API_BASE}/order/limit-short/${coin.symbol}?amount=${tempTradeAmount.value}&price=${priceVal}` : `${API_BASE}/order/market-short/${coin.symbol}?amount=${tempTradeAmount.value}`;
                const res = await fetch(endpoint, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && data.status === 'success') {
                    addLog(`✅ 做空成功! ID:${data.order.orderId} 數量:${parseFloat(data.order.executedQty).toFixed(5)}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) { addLog(`❌ 做空失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; limitPrice.value = ''; }
        };

        const placeMarketSell = async () => {
            const coin = activeCoin.value; if (!coin) return;
            isPlacingOrder.value = true;
            const priceVal = parseFloat(limitPrice.value) || 0;
            const isLimit = priceVal > 0;
            const orderTypeStr = isLimit ? `限價(${priceVal})` : '市價';

            addLog(`🛒 ${orderTypeStr}全倉賣出 ${coin.name}...`);
            try {
                const endpoint = isLimit ? `${API_BASE}/order/limit-sell/${coin.symbol}?price=${priceVal}` : `${API_BASE}/order/market-sell/${coin.symbol}`;
                const res = await fetch(endpoint, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && data.status === 'success') {
                    addLog(`✅ 賣出成功! ${data.order?.orderId || data.detail || ''}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) { addLog(`❌ 賣出失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; limitPrice.value = ''; }
        };

        const closeAllPositions = async () => {
            isPlacingOrder.value = true;
            addLog(`🔴 準備強制平倉所有持有部位...`);
            try {
                const res = await fetch(`${API_BASE}/order/close-all`, { method: 'POST' });
                const resp = await res.json();
                if (res.ok && resp.status === 'success') {
                    addLog(`✅ 一鍵全平倉成功! ${resp.detail}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(resp.detail || '平倉失敗');
            } catch(e) { addLog(`❌ 一鍵全平倉失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; }
        };

        const closeActivePosition = async () => {
            const coin = activeCoin.value; if (!coin) return;
            isPlacingOrder.value = true;
            addLog(`🔴 平倉 ${coin.name} (${activeSymbol.value})...`);
            try {
                const res = await fetch(`${API_BASE}/order/market-sell/${coin.symbol}`, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && data.status === 'success') {
                    addLog(`✅ 平倉成功! ${data.detail || data.order?.orderId || ''}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) { addLog(`❌ 平倉失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; }
        };

        const closePositionBySymbol = async (sym) => {
            isPlacingOrder.value = true;
            addLog(`🔴 平倉 ${sym}...`);
            try {
                const res = await fetch(`${API_BASE}/order/market-sell/${sym}`, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && (data.status === 'success' || data.detail)) {
                    addLog(`✅ 平倉成功! ${data.detail || data.order?.orderId || ''}`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) { addLog(`❌ 平倉失敗: ${e.message}`, 'danger'); }
            finally { isPlacingOrder.value = false; }
        };

        const getTradeCurrentPrice = (trade) => {
            const cp = parseFloat(trade.current_price);
            if (cp > 0) return cp;
            const sym = trade.symbol.replace(':USDT', 'USDT');
            const coin = coins.value.find(c => c.symbol === sym);
            return coin?.price || 0;
        };

        const getTradeUnrealizedPnl = (trade) => {
            if (trade.is_close) return 0;
            const pos = allPositions.value[trade.symbol];
            if (!pos || Math.abs(pos.qty) < 0.001) return 0;
            const currentPrice = getTradeCurrentPrice(trade);
            if (currentPrice <= 0) return 0;
            if (trade.isBuyer) return (currentPrice - trade.price) * trade.qty;
            return (trade.price - currentPrice) * trade.qty;
        };

        const getTradeUnrealizedPnlDisplay = (trade) => {
            const pnl = getTradeUnrealizedPnl(trade);
            if (pnl === 0) return '-';
            return pnl.toFixed(4);
        };

        const getTradeUnrealizedPnlPercent = (trade) => {
            const pnl = getTradeUnrealizedPnl(trade);
            if (pnl === 0 || trade.price * trade.qty === 0) return 0;
            return pnl / (trade.price * trade.qty) * 100 * (bot.value.leverage || 20);
        };

        const getTradeUnrealizedPnlPercentActual = (trade) => {
            const pnl = getTradeUnrealizedPnl(trade);
            if (pnl === 0 || trade.price * trade.qty === 0) return 0;
            return pnl / (trade.price * trade.qty) * 100;
        };

        const canCloseTrade = (trade) => {
            const sym = trade.symbol;
            return allPositions.value[sym] && Math.abs(allPositions.value[sym].qty) > 0;
        };

        const closeTradePosition = async (trade) => {
            const sym = trade.symbol.replace(':USDT', 'USDT');
            isPlacingOrder.value = true;
            showToast(`⏳ 平倉 ${sym}...`, 'info');
            addLog(`🔴 平倉 ${sym}...`);
            try {
                const res = await fetch(`${API_BASE}/order/market-sell/${sym}`, { method: 'POST' });
                const resp = await res.json();
                const data = resp.data || resp;
                if (res.ok && (data.status === 'success' || data.detail)) {
                    showToast(`✅ 平倉 ${sym} 完成!`, 'success');
                    addLog(`✅ 平倉 ${sym} 成功!`, 'success');
                    setTimeout(() => { fetchBotStatus(); fetchPosition(); fetchTrades(); fetchAllPositions(); }, 1200);
                } else throw new Error(data.detail);
            } catch(e) {
                addLog(`❌ 平倉 ${sym} 失敗: ${e.message}`, 'danger');
                showToast(`❌ ${e.message}`, 'danger', 5000);
            }
            finally { isPlacingOrder.value = false; }
        };

        // ── WebSocket ─────────────────────────────────────────────
        let wsFallbackTimeout, reconnectTimer, fallbackInterval;
        let isUsingFallback = false, lastLogTime = 0;
        let statusInterval, rateInterval, positionInterval, tradesInterval, logsInterval, klineInterval, allPositionsInterval, openOrdersInterval;

        const startHTTPFallback = () => {
            if (isUsingFallback) return;
            isUsingFallback = true;
            addLog('⚠️ WS 無回應，啟用 HTTP 輪詢...', 'warning');
            fallbackInterval = setInterval(async () => {
                for (let coin of coins.value) {
                    try {
                        const res = await fetch(`${API_BASE}/price/${coin.symbol}`);
                        if (res.ok) {
                            const d = await res.json();
                            coin.prevPrice = coin.price; coin.price = d.price;
                            if (coin.prevPrice > 0) {
                                coin.priceClass = coin.price > coin.prevPrice ? 'up' : coin.price < coin.prevPrice ? 'down' : '';
                                setTimeout(() => { coin.priceClass = ''; }, 400);
                            }
                            if (coin.symbol === activeSymbol.value) updateLatestCandle(coin.symbol, coin.price);
                        }
                    } catch(e) {}
                }
            }, 3000);
        };

        const wsConnections = [];

        const initWebSocket = () => {
            if (wsFallbackTimeout) clearTimeout(wsFallbackTimeout);
            if (reconnectTimer) clearTimeout(reconnectTimer);
            for (const w of wsConnections) { try { w.close(); } catch(e) {} }
            wsConnections.length = 0;

            wsFallbackTimeout = setTimeout(() => {
                const hasAnyPrice = coins.value.some(c => c.price > 0);
                if (!hasAnyPrice) startHTTPFallback();
            }, 8000);

            let connected = 0, failed = 0;
            for (const coin of coins.value) {
                const sym = coin.symbol.toLowerCase();
                const w = new WebSocket(`wss://fstream.binance.com/ws/${sym}@ticker`);
                w._coinSymbol = coin.symbol;
                w.onopen = () => { connected++; };
                w.onerror = () => { failed++; };
                w.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.e === '24hrTicker' && data?.s) {
                            if (wsFallbackTimeout) { clearTimeout(wsFallbackTimeout); wsFallbackTimeout = null; }
                            if (isUsingFallback) { isUsingFallback = false; clearInterval(fallbackInterval); addLog('⚡ WebSocket 恢復'); }
                            const c = coins.value.find(x => x.symbol === data.s);
                            if (c) {
                                c.prevPrice = c.price;
                                c.price = parseFloat(data.c);
                                if (c.prevPrice > 0) {
                                    c.priceClass = c.price > c.prevPrice ? 'up' : c.price < c.prevPrice ? 'down' : '';
                                    setTimeout(() => { c.priceClass = ''; }, 400);
                                }
                                if (data.s === activeSymbol.value) {
                                    updateLatestCandle(data.s, c.price);
                                    if (position.value.qty > 0) {
                                        position.value.current_price = c.price;
                                        position.value.current_value = position.value.qty * c.price;
                                        position.value.pnl = position.value.current_value - position.value.total_cost;
                                        position.value.pnl_percent = position.value.total_cost > 0 ? (position.value.pnl / position.value.total_cost) * 100 : 0;
                                    }
                                }
                            }
                        }
                    } catch(e) {}
                };
                w.onclose = () => {
                    failed++;
                    setTimeout(() => {
                        if (!isUsingFallback) {
                            const idx = wsConnections.indexOf(w);
                            if (idx > -1) { wsConnections.splice(idx, 1); }
                            const nw = new WebSocket(`wss://fstream.binance.com/ws/${sym}@ticker`);
                            nw._coinSymbol = coin.symbol;
                            // re-assign same event handlers
                            nw.onopen = w.onopen;
                            nw.onerror = w.onerror;
                            nw.onmessage = w.onmessage;
                            nw.onclose = w.onclose;
                            wsConnections.push(nw);
                        }
                    }, 10000);
                };
                wsConnections.push(w);
            }
        };

        // Backend logs sync
        const fetchBackendLogs = async () => {
            try {
                const res = await fetch(`${API_BASE}/logs`);
                if (!res.ok) return;
                const bl = await res.json();
                let hasNew = false;
                for (let log of bl) {
                    let lc = log.level === 'warning' ? 'log-warning' : log.level === 'danger' ? 'log-danger' : log.level === 'success' ? 'log-success' : 'log-info';
                    if (!logs.value.some(l => l.time === log.time && l.text === log.text)) {
                        logs.value.push({ time: log.time, text: log.text, levelClass: lc });
                        hasNew = true;
                    }
                }
                if (hasNew) {
                    if (logs.value.length > 100) logs.value = logs.value.slice(-100);
                    nextTick(() => { if (consoleRef.value) consoleRef.value.scrollTop = consoleRef.value.scrollHeight; });
                }
            } catch(e) {}
        };

        const fetchAllTradesHistory = async () => {
            // Implementation logic
        };

        onMounted(() => {
            addLog('🚀 Dashboard 初始化...');
            initChart();
            fetchExchangeRate();
            fetchBotStatus();
            fetchPosition();
            fetchTrades();
            fetchOpenOrders();
            loadKlines();
            initWebSocket();
            initLiveTradesWs();
            fetchBackendLogs();

            statusInterval       = setInterval(fetchBotStatus, 5000);
            positionInterval     = setInterval(fetchPosition, 5000);
            allPositionsInterval = setInterval(fetchAllPositions, 3000);
            tradesInterval       = setInterval(fetchTrades, 8000);
            rateInterval         = setInterval(fetchExchangeRate, 60000);
            logsInterval         = setInterval(fetchBackendLogs, 2000);
            klineInterval        = setInterval(loadKlines, 60000);
            openOrdersInterval   = setInterval(fetchOpenOrders, 5000);
        });

        onUnmounted(() => {
            for (const w of wsConnections) { try { w.close(); } catch(e) {} }
            if (wsFallbackTimeout) clearTimeout(wsFallbackTimeout);
            if (reconnectTimer)    clearTimeout(reconnectTimer);
            if (fallbackInterval)  clearInterval(fallbackInterval);
            clearInterval(statusInterval); clearInterval(positionInterval);
            clearInterval(tradesInterval); clearInterval(rateInterval);
            clearInterval(logsInterval);   clearInterval(klineInterval);
            clearInterval(allPositionsInterval); clearInterval(openOrdersInterval);
        });

        // ── Submit limit / market order from the new panel ──────────
        const submitLimitOrder = async () => {
            if (loSide.value === 'long') {
                await placeMarketBuy();
            } else {
                await placeMarketShort();
            }
        };

        const fetchHistorySummary = async () => {
            try {
                const res = await fetch(`${API_BASE}/history/summary`);
                if (res.ok) {
                    const data = await res.json();
                    historySummaries.value = data.summaries || [];
                }
            } catch (e) {
                console.error("Failed to fetch history summary", e);
            }
        };

        const downloadHistory = (date) => {
            window.location.href = `${API_BASE}/history/download/${date}`;
        };

        return {
            customSymbol, addCustomCoin, coins, sortedCoins, activeSymbol, activeCoin,
            formattedMainPrice, mainPriceClass,
            currentCurrency, currentCurrencySymbol, twdRate,
            toggleCurrency, setActiveCoin,
            formatMiniPrice, formatCurrencyVal, formatTradeTime, formatLiveTradeTime, getActiveCoinLabel, getQuoteAsset, formatQuoteBalance,
            bot, position, trades, liveTrades, error,
            toggleBot, isPlacingOrder, placeMarketBuy, placeMarketShort, placeMarketSell, closeActivePosition, closePositionBySymbol, closeAllPositions, closeTradePosition, canCloseTrade, getTradeUnrealizedPnl, getTradeUnrealizedPnlDisplay, getTradeUnrealizedPnlPercent, getTradeUnrealizedPnlPercentActual, limitPrice,
            cancelAllOrders, cancelOrders, autoSnipeBestCoin, logs, consoleRef, clearLogs, copyLogs,
            tempTradeAmount, updateTradeAmount,
            selectedTf, timeframes, changeTf,
            volumeBars, latestVolumeStr, volumeUnit, high24h, low24h,
            allPositions, openOrders,
            showLogs,
            showAllTradesModal, allTradesHistory, fetchAllTradesHistory,
            loSide, loOrderType, loTpPct, loSlPct, submitLimitOrder,
            showHistoryNotebook, historySummaries, fetchHistorySummary, downloadHistory,
            toast, showToast
        };
    }
}).mount('#app');

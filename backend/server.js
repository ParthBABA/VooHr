require('dotenv').config();

const path = require('path');
const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const morgan = require('morgan');
const cookieParser = require('cookie-parser');

const connectDB = require('./config/db');
const authRoutes = require('./routes/auth.routes');
const orgRoutes = require('./routes/org.routes');
const userRoutes = require('./routes/user.routes');
const errorHandler = require('./middleware/errorHandler');

const app = express();
const PORT = process.env.PORT || 4000;
const CLIENT_URL = process.env.CLIENT_URL || 'http://localhost:5500';

app.use(helmet({ contentSecurityPolicy: false })); // CSP off in dev; tighten for prod
app.use(cors({ origin: CLIENT_URL, credentials: true }));
app.use(morgan('dev'));
app.use(express.json());
app.use(cookieParser());

// Serve the existing static frontend as-is (adjust/remove if you deploy it separately)
app.use(express.static(path.join(__dirname, '..', 'static')));

app.use('/auth', authRoutes);
app.use('/api/organizations', orgRoutes);
app.use('/api', userRoutes);

app.get('/healthz', (req, res) => res.json({ ok: true }));

app.use(errorHandler);

async function start() {
  try {
    await connectDB();
    app.listen(PORT, () => {
      console.log(`[server] listening on http://localhost:${PORT}`);
    });
  } catch (err) {
    console.error('[server] failed to start:', err.message);
    process.exit(1);
  }
}

start();

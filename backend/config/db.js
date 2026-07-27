const mongoose = require('mongoose');

async function connectDB() {
  const uri = process.env.MONGODB_URI;
  if (!uri) {
    throw new Error('MONGODB_URI is not set in the environment');
  }

  mongoose.set('strictQuery', true);

  await mongoose.connect(uri, {
    // Atlas connection strings already carry all needed params (retryWrites, w=majority, etc.)
    // Mongoose 8 no longer needs useNewUrlParser/useUnifiedTopology, they're default.
  });

  console.log(`[db] connected to MongoDB Atlas -> ${mongoose.connection.name}`);

  mongoose.connection.on('error', (err) => {
    console.error('[db] connection error:', err.message);
  });
  mongoose.connection.on('disconnected', () => {
    console.warn('[db] disconnected');
  });
}

module.exports = connectDB;

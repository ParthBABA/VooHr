function errorHandler(err, req, res, next) {
  console.error('[error]', err);

  if (res.headersSent) return next(err);

  const status = err.status || 500;
  res.status(status).json({
    error: err.publicMessage || 'server_error',
    message: process.env.NODE_ENV === 'production' ? undefined : err.message,
  });
}

module.exports = errorHandler;

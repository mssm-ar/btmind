const path = require('path');
const fs = require('fs');

const projectRoot = __dirname;
const venvPython = path.join(projectRoot, '.venv', 'bin', 'python');
const python = fs.existsSync(venvPython) ? venvPython : 'python3';

module.exports = {
  apps: [
    {
      name: 'openrouter-balance-bot',
      cwd: projectRoot,
      script: path.join(projectRoot, 'openrouter_balance_bot.py'),
      interpreter: python,
      args: '--watch 3600',
      watch: false,
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      restart_delay: 4000,
      time: true,
    },
  ],
};

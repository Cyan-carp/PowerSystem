const { app } = require('electron');
const path = require('node:path');
app.setPath('userData', path.resolve(process.env.POWERSYSTEM_TEST_DATA));
require('../dist/main.js');

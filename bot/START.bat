@echo off
title CLAUDE + QUANT Bot
echo Installing dependencies...
pip install MetaTrader5 -q
echo.
echo Starting bot...
python trading_bot.py
pause

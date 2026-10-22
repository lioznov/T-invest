const test=require('node:test');
const assert=require('node:assert/strict');
const {safeLink,TOPICS,percent,TICKERS}=require('../static/js/news.js');
test('only safe HTTPS links are interactive',()=>{
  for(const value of ['javascript:alert(1)','file:///secret','data:text/html,x','http://example.com','https://user:secret@example.com','https://example.com:8080'])assert.equal(safeLink(value),null);
  assert.equal(safeLink('https://www.cbr.ru/news/1'),'https://www.cbr.ru/news/1');
});
test('topics are explanations, not LONG probabilities',()=>{
  assert.equal(TOPICS.dividends,'Дивиденды');
  assert.equal(Object.hasOwn(TOPICS,'LONG'),false);
});
test('price changes do not replace missing values with zero',()=>{
  for(const value of [null,undefined,NaN,Infinity,'1'])assert.equal(percent(value),'—');
  assert.equal(percent(1.5),'+1.50%');
  assert.equal(percent(-.2),'-0.20%');
});
test('news context uses a bounded supported universe',()=>{
  assert.equal(TICKERS.length,7);assert.ok(TICKERS.includes('SBER'));
});

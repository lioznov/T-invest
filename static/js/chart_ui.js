(function(root){
  'use strict';
  // Presentation only: indicator values, signals and candle history are unchanged.
  function chartLayout(width,height){
    const w=Number.isFinite(width)&&width>0?Math.max(240,Math.round(width)):800;
    const h=Number.isFinite(height)&&height>0?Math.max(200,Math.round(height)):310;
    return {width:w,height:h,left:12,right:72,top:22,bottom:34,ticks:w<500?3:4};
  }
  function scenarioPrice(value){
    if(!Number.isFinite(value)||value<=0)return '';
    // Kopecks for ordinary share prices; retain small-price precision rather than
    // rendering a sub-rouble instrument as zero. This is not tick-size validation.
    const text=value.toFixed(value>=1?2:8);
    if(Number(text)===0)return String(value);
    return value>=1?text:text.replace(/0+$/,'').replace(/\.$/,'');
  }
  const api={chartLayout,scenarioPrice};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.VorbyChartUI=api;
})(typeof window==='object'?window:globalThis);

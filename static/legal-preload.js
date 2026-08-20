(function(){
  var ASSET='Banding - Copy@1-1536x730.png';
  var TARGETS={'/privacy-policy.html':1,'/terms-of-service.html':1};
  var cache=null;
  var navigating=false;

  function preloadLegalBg(){
    if(cache) return Promise.resolve();
    return new Promise(function(ok){
      var img=new Image();
      img.onload=function(){cache=1;ok()};
      img.onerror=function(){ok()};
      img.src=ASSET;
    });
  }

  function resolveTarget(el){
    var a=el.closest&&el.closest('a');
    if(!a||!a.href||a.target==='_blank'||a.hasAttribute('download')) return null;
    try{
      var u=new URL(a.href,location.href);
      if(u.origin!==location.origin) return null;
      if(!TARGETS[u.pathname]) return null;
      return u.href;
    }catch(e){return null}
  }

  document.addEventListener('click',function(e){
    if(navigating||e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey) return;
    var href=resolveTarget(e.target);
    if(!href) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    navigating=true;
    preloadLegalBg().then(function(){location.href=href});
  },true);

  window.preloadLegalBg=preloadLegalBg;
})();

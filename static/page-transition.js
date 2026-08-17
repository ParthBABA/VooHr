/* Page-transition overlay controller.
   Handles link interception (fade-out before navigation) and
   reveal-on-load (wait for fonts then fade-out the overlay).
   Shared across all onboarding funnel pages + login.html. */
(function(){
 var o=document.getElementById('voovr-page-transition'),seen=new Set(),ht;
 function u(a){
   if(!a||!a.href||a.target==='_blank'||a.hasAttribute('download')||a.dataset.noPrefetch!==undefined)return null;
   try{var x=new URL(a.href,location.href);return x.origin===location.origin&&x.href!==location.href?x:null}catch(e){return null}
 }
 function pre(x){
   if(!x||seen.has(x.href))return; seen.add(x.href);
   if(navigator.connection&&navigator.connection.saveData)return;
   try{var l=document.createElement('link');l.rel='prefetch';l.href=x.href;l.as='document';document.head.appendChild(l)}catch(e){}
 }
 document.addEventListener('pointerover',function(e){var x=u(e.target.closest&&e.target.closest('a'));if(x){clearTimeout(ht);ht=setTimeout(function(){pre(x)},60)}},{passive:true});
 document.addEventListener('focusin',function(e){var x=u(e.target.closest&&e.target.closest('a'));if(x)pre(x)});
 document.addEventListener('click',function(e){
   if(e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
   var a=e.target.closest&&e.target.closest('a'),x=u(a);if(!x||a.dataset.transition==='off')return;
   e.preventDefault();if(o)o.classList.add('is-active');requestAnimationFrame(function(){location.href=x.href});
 },true);

 async function reveal(){
   try{if(document.fonts&&document.fonts.ready)await document.fonts.ready}catch(e){}
   requestAnimationFrame(function(){requestAnimationFrame(function(){if(o)o.classList.remove('is-active')})});
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',reveal,{once:true});else reveal();
 window.addEventListener('pageshow',function(){if(o)o.classList.remove('is-active')},{once:true});
})();

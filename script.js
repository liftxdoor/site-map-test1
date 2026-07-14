
const toggle=document.querySelector('.menu-toggle');
const menu=document.querySelector('.mobile-menu');
if(toggle&&menu){
 toggle.addEventListener('click',()=>{
   const open=menu.classList.toggle('open');
   document.body.classList.toggle('menu-open',open);
   toggle.setAttribute('aria-expanded',String(open));
 });
 menu.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{
   menu.classList.remove('open');document.body.classList.remove('menu-open');
 }));
}
const params=new URLSearchParams(location.search);
const type=params.get('type');
const select=document.querySelector('select[name="customer_type"]');
if(type&&select){
 const map={builder:'Builder / Contractor',property:'Property Manager',commercial:'Business / Commercial',homeowner:'Homeowner'};
 [...select.options].forEach(o=>{if(o.text===map[type])o.selected=true});
}

/*
 * UniFi Toolkit translations (English is the source language).
 *
 * The tools render English in templates and in Alpine/vanilla JS. Instead of
 * rewriting every template, this translates the page in the browser: every
 * text node and the title/placeholder/aria-label/alt attributes are looked up
 * in the dictionary for the chosen language (exact match on the trimmed,
 * whitespace-collapsed text, then the regex patterns). A MutationObserver
 * keeps text that the tools render later (tables, toasts, modals) translated.
 *
 * Dictionaries live in /static/i18n/<lang>.js and register themselves on
 * window.TOOLKIT_I18N[lang] = { strings: {...}, patterns: [[regex, replacement], ...] }.
 */
(function () {
    var KEY = 'unifi-toolkit-lang';
    var LANGS = { en: 'English', nl: 'Nederlands' };
    var lang = 'en';
    try { lang = localStorage.getItem(KEY) || (document.cookie.match(/(?:^|; )toolkit_lang=(\w+)/) || [])[1] || 'en'; } catch (e) {}
    if (!LANGS[lang]) lang = 'en';
    document.documentElement.lang = lang;

    window.toolkitLang = {
        current: lang,
        languages: LANGS,
        set: function (next) {
            try { localStorage.setItem(KEY, next); } catch (e) {}
            document.cookie = 'toolkit_lang=' + next + '; path=/; max-age=31536000; SameSite=Lax';
            location.reload();
        },
        t: function (s) { return translate(s); }
    };
    if (lang === 'en') return;

    // Hide the page until the first pass so English doesn't flash.
    document.documentElement.classList.add('i18n-pending');
    setTimeout(function () { document.documentElement.classList.remove('i18n-pending'); }, 1500);

    var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
    var dict = null, patterns = [];

    function load() {
        var d = (window.TOOLKIT_I18N || {})[lang];
        if (!d) return false;
        dict = d.strings || {};
        patterns = (d.patterns || []).map(function (p) { return [new RegExp('^' + p[0] + '$'), p[1]]; });
        return true;
    }

    function translate(text) {
        if (!dict) return text;
        var core = text.replace(/\s+/g, ' ').trim();
        if (!core || !/[A-Za-z]/.test(core)) return text;
        var hit = dict[core];
        if (hit === undefined) {
            for (var i = 0; i < patterns.length; i++) {
                if (patterns[i][0].test(core)) { hit = core.replace(patterns[i][0], patterns[i][1]); break; }
            }
        }
        if (hit === undefined) return text;
        var lead = text.match(/^\s*/)[0], trail = text.match(/\s*$/)[0];
        return lead + hit + trail;
    }

    function skip(el) {
        return el && (el.closest && el.closest('script,style,code,pre,textarea,[data-no-i18n]'));
    }

    function doText(node) {
        if (skip(node.parentElement)) return;
        var v = node.nodeValue, t = translate(v);
        if (t !== v) node.nodeValue = t;
    }

    function doEl(el) {
        if (skip(el)) return;
        for (var i = 0; i < ATTRS.length; i++) {
            var a = el.getAttribute(ATTRS[i]);
            if (a) { var t = translate(a); if (t !== a) el.setAttribute(ATTRS[i], t); }
        }
        if ((el.tagName === 'INPUT') && (el.type === 'button' || el.type === 'submit') && el.value) {
            var tv = translate(el.value); if (tv !== el.value) el.value = tv;
        }
    }

    function walk(root) {
        if (root.nodeType === 3) { doText(root); return; }
        if (root.nodeType !== 1 && root.nodeType !== 9) return;
        if (root.nodeType === 1) doEl(root);
        var w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
        var n;
        while ((n = w.nextNode())) { if (n.nodeType === 3) doText(n); else doEl(n); }
    }

    function start() {
        if (!load()) { document.documentElement.classList.remove('i18n-pending'); return; }
        document.title = translate(document.title);
        walk(document.body);
        document.documentElement.classList.remove('i18n-pending');
        new MutationObserver(function (muts) {
            for (var i = 0; i < muts.length; i++) {
                var m = muts[i];
                if (m.type === 'characterData') doText(m.target);
                else if (m.type === 'attributes') doEl(m.target);
                else for (var j = 0; j < m.addedNodes.length; j++) walk(m.addedNodes[j]);
            }
        }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
        // Native dialogs from the tools.
        var _confirm = window.confirm, _alert = window.alert;
        window.confirm = function (m) { return _confirm.call(window, translate(String(m))); };
        window.alert = function (m) { return _alert.call(window, translate(String(m))); };
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();

const normalizeValue = (value) => value.trim().toLowerCase();
const escapeHtml = (value) => value
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#39;');

function initPicker(root) {
  const input = root.querySelector('.picker-input');
  const menu = root.querySelector('.picker-menu');
  const toggle = root.querySelector('.picker-toggle');
  const allowCustom = root.dataset.allowCustom !== 'false';
  const options = JSON.parse(root.dataset.options || '[]');

  const renderOptions = (query = '') => {
    const normalizedQuery = normalizeValue(query);
    const fragments = [];
    const matches = options.filter((option) => {
      if (option.kind === 'subcategory' && normalizedQuery) {
        return normalizeValue(option.label).includes(normalizedQuery)
          || normalizeValue(option.value).includes(normalizedQuery)
          || normalizeValue(option.group || '').includes(normalizedQuery);
      }
      return !normalizedQuery || normalizeValue(option.label).includes(normalizedQuery) || normalizeValue(option.value || '').includes(normalizedQuery);
    });

    let currentGroup = null;
    matches.forEach((option) => {
      const safeLabel = escapeHtml(option.label);
      const safeValue = escapeHtml(option.value || '');
      if (option.kind === 'category') {
        if (currentGroup !== option.label) {
          fragments.push(`<div class="picker-header">${safeLabel}</div>`);
          currentGroup = option.label;
        }
        return;
      }
      if (option.kind === 'subcategory') {
        if (currentGroup !== option.group) {
          fragments.push(`<div class="picker-header">${escapeHtml(option.group || '')}</div>`);
          currentGroup = option.group;
        }
        fragments.push(`<button type="button" class="picker-option subcategory" data-value="${safeValue}">${safeLabel}</button>`);
        return;
      }
      if (option.group) {
        if (currentGroup !== option.group) {
          fragments.push(`<div class="picker-header">${escapeHtml(option.group)}</div>`);
          currentGroup = option.group;
        }
      } else {
        currentGroup = null;
      }
      fragments.push(`<button type="button" class="picker-option" data-value="${safeValue}">${safeLabel}</button>`);
    });

    const hasExact = matches.some((option) => normalizeValue(option.value || '') === normalizedQuery);
    if (allowCustom && query.trim() && !hasExact) {
      const safeQuery = escapeHtml(query.trim());
      fragments.push(`<button type="button" class="picker-create" data-value="${safeQuery}">Add “${safeQuery}”</button>`);
    }

    menu.innerHTML = fragments.join('') || '<div class="picker-header">No matches</div>';
  };

  const openMenu = () => {
    renderOptions(input.value);
    root.classList.add('is-open');
  };

  const closeMenu = () => root.classList.remove('is-open');
  const selectValue = (value) => {
    input.value = value;
    closeMenu();
    input.dispatchEvent(new Event('input', { bubbles: true }));
  };

  input.addEventListener('focus', openMenu);
  input.addEventListener('input', () => {
    renderOptions(input.value);
    root.classList.add('is-open');
  });
  toggle.addEventListener('click', () => {
    if (root.classList.contains('is-open')) {
      closeMenu();
    } else {
      openMenu();
      input.focus();
    }
  });
  menu.addEventListener('pointerdown', (event) => {
    const option = event.target.closest('[data-value]');
    if (!option) {
      return;
    }
    event.preventDefault();
    selectValue(option.dataset.value || '');
  });
  menu.addEventListener('click', (event) => {
    const option = event.target.closest('[data-value]');
    if (!option) {
      return;
    }
    selectValue(option.dataset.value || '');
  });
  input.addEventListener('blur', () => {
    window.setTimeout(closeMenu, 120);
  });
}

function scrollStorageKey(key) {
  return `scroll:${key}`;
}

function saveScrollPosition(key) {
  if (!key) {
    return;
  }
  window.sessionStorage.setItem(scrollStorageKey(key), String(window.scrollY));
}

function initScrollRestoration() {
  const scrollRoot = document.querySelector('[data-scroll-restore-key]');
  if (!scrollRoot) {
    return;
  }
  const key = scrollRoot.dataset.scrollRestoreKey;
  const saved = window.sessionStorage.getItem(scrollStorageKey(key));
  if (saved !== null) {
    window.scrollTo(0, Number(saved));
    window.sessionStorage.removeItem(scrollStorageKey(key));
  }
  document.querySelectorAll('[data-preserve-scroll]').forEach((node) => {
    const save = () => saveScrollPosition(key);
    if (node.tagName === 'FORM') {
      node.addEventListener('submit', save);
    } else {
      node.addEventListener('click', save);
    }
  });
}

function initCategoryDialog() {
  const dialog = document.querySelector('[data-category-dialog]');
  const openButton = document.querySelector('[data-open-category-dialog]');
  const applyButton = dialog?.querySelector('[data-apply-category-dialog]');
  const groupInput = dialog?.querySelector('[data-category-group-input]');
  const subcategoryInput = dialog?.querySelector('[data-category-subcategory-input]');
  const targetInput = document.querySelector('input[name="category_value"]');

  if (!dialog || !openButton || !applyButton || !groupInput || !subcategoryInput || !targetInput) {
    return;
  }

  openButton.addEventListener('click', () => {
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    }
  });

  applyButton.addEventListener('click', () => {
    const group = groupInput.value.trim();
    const subcategory = subcategoryInput.value.trim();
    if (!group || !subcategory) {
      return;
    }
    targetInput.value = `${group} / ${subcategory}`;
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    dialog.close();
  });
}

function initSelectableFormFields() {
  const canSelectAll = (node) => {
    if (!(node instanceof HTMLInputElement)) {
      return false;
    }
    if (!node.value.trim()) {
      return false;
    }
    return !['hidden', 'file', 'date', 'checkbox', 'radio'].includes(node.type);
  };

  document.querySelectorAll('[data-select-on-focus]').forEach((input) => {
    const selectAll = () => {
      if (!canSelectAll(input)) {
        return;
      }
      input.dataset.autoSelectPending = 'true';
      window.requestAnimationFrame(() => {
        if (document.activeElement !== input || !canSelectAll(input)) {
          input.dataset.autoSelectPending = '';
          return;
        }
        input.select();
      });
    };

    input.addEventListener('focus', selectAll);
    input.addEventListener('pointerup', (event) => {
      if (input.dataset.autoSelectPending !== 'true' || !canSelectAll(input)) {
        return;
      }
      event.preventDefault();
      input.select();
      input.dataset.autoSelectPending = '';
    });
    input.addEventListener('blur', () => {
      input.dataset.autoSelectPending = '';
    });
  });
}

function initSelectionMode() {
  const root = document.querySelector('[data-selection-root]');
  const bulkForm = document.querySelector('[data-bulk-form]');
  if (!root || !bulkForm) {
    return;
  }

  const selected = new Set();
  const scrollKey = root.dataset.scrollRestoreKey;
  const inputsRoot = bulkForm.querySelector('[data-selection-inputs]');
  const countNode = bulkForm.querySelector('[data-selection-count]');
  const clearButton = bulkForm.querySelector('[data-clear-selection]');
  let selectionMode = false;

  const syncState = () => {
    root.querySelectorAll('[data-transaction-id]').forEach((row) => {
      row.classList.toggle('is-selected', selected.has(row.dataset.transactionId));
    });
    root.querySelectorAll('[data-date-group]').forEach((group) => {
      const rowIds = [...group.querySelectorAll('[data-transaction-id]')].map((row) => row.dataset.transactionId).filter(Boolean);
      const allSelected = rowIds.length > 0 && rowIds.every((id) => selected.has(id));
      group.classList.toggle('is-selected', allSelected);
    });
    inputsRoot.innerHTML = [...selected]
      .map((value) => `<input type="hidden" name="transaction_ids" value="${escapeHtml(value)}">`)
      .join('');
    countNode.textContent = `${selected.size} selected`;
    selectionMode = selected.size > 0;
    bulkForm.classList.toggle('is-active', selectionMode);
    root.classList.toggle('has-selection', selectionMode);
  };

  const toggleSelection = (row) => {
    const id = row.dataset.transactionId;
    if (!id) {
      return;
    }
    if (selected.has(id)) {
      selected.delete(id);
    } else {
      selected.add(id);
    }
    syncState();
  };

  clearButton?.addEventListener('click', () => {
    selected.clear();
    syncState();
  });

  root.querySelectorAll('[data-transaction-id]').forEach((row) => {
    let longPressTimer = null;
    let longPressed = false;

    const startPress = () => {
      longPressed = false;
      longPressTimer = window.setTimeout(() => {
        longPressed = true;
        toggleSelection(row);
      }, 420);
    };

    const clearPress = () => {
      if (longPressTimer) {
        window.clearTimeout(longPressTimer);
        longPressTimer = null;
      }
    };

    row.addEventListener('pointerdown', startPress);
    row.addEventListener('pointerup', clearPress);
    row.addEventListener('pointerleave', clearPress);
    row.addEventListener('pointercancel', clearPress);
    row.addEventListener('contextmenu', (event) => event.preventDefault());
    row.addEventListener('click', (event) => {
      const interactiveTarget = event.target.closest('a, button, input');
      if (interactiveTarget) {
        return;
      }
      if (longPressed || selectionMode) {
        event.preventDefault();
        toggleSelection(row);
        longPressed = false;
        return;
      }
      const editUrl = row.dataset.editUrl;
      if (editUrl) {
        saveScrollPosition(scrollKey);
        window.location.href = editUrl;
      }
    });
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (selectionMode) {
          toggleSelection(row);
          return;
        }
        const editUrl = row.dataset.editUrl;
        if (editUrl) {
          saveScrollPosition(scrollKey);
          window.location.href = editUrl;
        }
      }
    });
  });

  root.querySelectorAll('[data-date-group-toggle]').forEach((toggle) => {
    toggle.addEventListener('click', () => {
      if (!selectionMode) {
        return;
      }
      const group = toggle.closest('[data-date-group]');
      const rowIds = [...(group?.querySelectorAll('[data-transaction-id]') || [])]
        .map((row) => row.dataset.transactionId)
        .filter(Boolean);
      const shouldSelectAll = rowIds.some((id) => !selected.has(id));
      rowIds.forEach((id) => {
        if (shouldSelectAll) {
          selected.add(id);
        } else {
          selected.delete(id);
        }
      });
      syncState();
    });
  });
}

function initInlineEditors() {
  document.querySelectorAll('[data-inline-edit-open]').forEach((button) => {
    button.addEventListener('click', () => {
      const row = button.closest('.manage-row, .manage-group');
      const form = row?.querySelector('.manage-inline-form');
      const input = form?.querySelector('[data-inline-edit-input]');
      if (!row || !form || !input) {
        return;
      }
      row.classList.add('is-editing');
      form.hidden = false;
      input.dataset.originalValue = input.value;
      window.setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    });
  });

  document.querySelectorAll('[data-inline-edit-input]').forEach((input) => {
    const finishEdit = (submitChange) => {
      const form = input.closest('.manage-inline-form');
      const row = input.closest('.manage-row, .manage-group');
      if (!row || !form) {
        return;
      }
      const nextValue = input.value.trim();
      const originalValue = input.dataset.originalValue || '';
      if (!submitChange || !nextValue || nextValue === originalValue) {
        input.value = originalValue;
        form.hidden = true;
        row.classList.remove('is-editing');
        return;
      }
      form.requestSubmit();
    };

    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        finishEdit(true);
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        finishEdit(false);
      }
    });
    input.addEventListener('blur', () => {
      window.setTimeout(() => finishEdit(true), 60);
    });
  });
}

function initConfirmActions() {
  document.addEventListener('submit', (event) => {
    const submitter = event.submitter;
    const message = submitter?.dataset.confirmMessage;
    if (!message) {
      return;
    }
    if (!window.confirm(message)) {
      event.preventDefault();
    }
  });
}

function initManageDeleteDialogs() {
  const dialog = document.querySelector('[data-manage-delete-dialog]');
  if (!dialog) {
    return;
  }

  const titleNode = dialog.querySelector('[data-delete-title]');
  const messageNode = dialog.querySelector('[data-delete-message]');
  const currentNameInput = dialog.querySelector('[data-delete-current-name]');
  const categoryNameInput = dialog.querySelector('[data-delete-category-name]');
  const strategyInput = dialog.querySelector('[data-delete-strategy]');
  const actionInput = dialog.querySelector('[data-delete-action]');
  const migrationFields = dialog.querySelector('[data-delete-migration-fields]');
  const submitButton = dialog.querySelector('[data-delete-submit]');
  const deleteTransactionsButton = dialog.querySelector('[data-delete-switch="delete_transactions"]');
  const closeButton = dialog.querySelector('[data-close-manage-delete]');
  const replacementInput = dialog.querySelector('input[name="replacement_name"], input[name="replacement_category_value"]');

  const closeDialog = () => {
    if (typeof dialog.close === 'function') {
      dialog.close();
    }
  };

  closeButton?.addEventListener('click', closeDialog);

  document.querySelectorAll('[data-manage-delete]').forEach((button) => {
    button.addEventListener('click', () => {
      const count = Number(button.dataset.deleteCount || '0');
      const kind = button.dataset.deleteKind || 'item';
      const name = button.dataset.deleteName || '';
      const category = button.dataset.deleteCategory || '';
      if (titleNode) {
        titleNode.textContent = `Delete ${kind}`;
      }
      if (messageNode) {
        messageNode.textContent = count > 0
          ? `${name} is still used by ${count} transaction${count === 1 ? '' : 's'}. Migrate those transactions or delete them with this ${kind}.`
          : `Delete ${name}?`;
      }
      if (currentNameInput) {
        currentNameInput.value = name;
      }
      if (categoryNameInput) {
        categoryNameInput.value = category;
      }
      if (actionInput) {
        actionInput.value = kind === 'subcategory' ? 'delete-subcategory' : `delete-${kind}`;
      }
      if (strategyInput) {
        strategyInput.value = count > 0 ? 'migrate' : '';
      }
      if (replacementInput) {
        replacementInput.value = '';
      }
      if (migrationFields) {
        migrationFields.hidden = count === 0;
      }
      if (submitButton) {
        submitButton.textContent = count > 0 ? 'Migrate & delete' : 'Delete';
      }
      if (deleteTransactionsButton) {
        deleteTransactionsButton.hidden = count === 0;
      }
      if (typeof dialog.showModal === 'function') {
        dialog.showModal();
      }
    });
  });

  deleteTransactionsButton?.addEventListener('click', () => {
    if (strategyInput) {
      strategyInput.value = 'delete_transactions';
    }
    dialog.querySelector('form')?.requestSubmit();
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initScrollRestoration();
  document.querySelectorAll('.js-picker').forEach(initPicker);
  initSelectableFormFields();
  initCategoryDialog();
  initSelectionMode();
  initInlineEditors();
  initConfirmActions();
  initManageDeleteDialogs();
});

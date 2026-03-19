(() => {
    const board = document.getElementById("kanban-board");
    if (!board) {
        return;
    }

    const canDrag = board.dataset.canDrag === "1";
    if (!canDrag || typeof Sortable === "undefined") {
        return;
    }

    const moveUrlTemplate = board.dataset.moveUrlTemplate || "";
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";

    const refreshCounters = () => {
        document.querySelectorAll(".kanban-column").forEach((column) => {
            const list = column.querySelector(".kanban-list");
            const badge = column.querySelector(".badge");
            if (list && badge) {
                badge.textContent = String(list.children.length);
            }
        });
    };

    const buildMoveUrl = (taskId) => moveUrlTemplate.replace("/0/", `/${taskId}/`);

    const saveMove = async (taskId, statusId) => {
        const response = await fetch(buildMoveUrl(taskId), {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify({ status_id: Number(statusId) }),
        });

        if (!response.ok) {
            throw new Error("Move request failed");
        }
    };

    document.querySelectorAll(".kanban-list").forEach((list) => {
        new Sortable(list, {
            group: "kanban",
            animation: 180,
            ghostClass: "sortable-ghost",
            onEnd: async (evt) => {
                const taskId = evt.item?.dataset?.taskId;
                const statusId = evt.to?.dataset?.statusId;
                if (!taskId || !statusId || evt.from === evt.to) {
                    refreshCounters();
                    return;
                }

                try {
                    await saveMove(taskId, statusId);
                    refreshCounters();
                } catch (_error) {
                    window.alert("Не удалось изменить статус. Доска будет перезагружена.");
                    window.location.reload();
                }
            },
        });
    });
})();
